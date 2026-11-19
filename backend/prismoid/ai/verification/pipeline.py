# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Run a verifier suite in a sandbox, and turn its stdout into `Check` values.

**A fresh frame, not the step's frame** - and this is the design decision in the file.

The design says the suite runs "in the frame against the published bytes", and the reason is sound: opening
an `.xlsx` the model wrote, or *rebuilding a program* the model wrote, is not something you do in
the process holding the database connection. But the step's frame is territory the agent controlled
for the whole of its run. It could not touch its sources (they are read-only to it), and it could not
reach the network - but it could leave a `sitecustomize.py` on the interpreter's path, or a
`python3` earlier in `PATH`, and then the verifier is a program the accused compiled.

**A verifier that runs inside the environment it is verifying can be lied to.** So it gets a clean
one: a frame with no network, the published bytes, the source bytes, and nothing else - 155 ms, and
the question stops being interesting.

It is also the only thing that *can* work. `prismoid artifacts verify $AID` is answerable a week
after the task finished, and the step's frame was destroyed the moment the step ended. A design
that could only verify during the step could not verify at all.

`render_pages` shows a vision judge what a person opens: a paged file's pages, rendered in the same
fresh frame by the house renderer (LibreOffice, then poppler) and sampled as it samples them.

`final_verdict` lets the rubric follow the work: the suite proves a figure derivable, never that it
means anything, so the judge may fail what the suite passed but never lift a FAIL. An unsourced
document leaves the suite nothing substantive to examine, so there the judge's PASS stands - only
when it was shown the content, since a PASS over a binary's placeholder signs bytes nobody read.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

import logfire
from pydantic import ValidationError

from prismoid.ai.frames.driver import run_python
from prismoid.ai.frames.layout import ARTIFACTS, WORK
from prismoid.schemas.claims import GROUNDING, Grounding
from prismoid.schemas.verification import (
    TESTS_RUN,
    Check,
    CheckKind,
    CheckStatus,
    CodeReceipt,
    JudgeOpinion,
    Verdict,
    VerifierSuite,
)

if TYPE_CHECKING:
    from prismoid.ai.frames.driver import FrameDriver

SUITES = Path(__file__).parent / "suites"
# What a suite imports from a sibling: a script runs alone in the frame, so its siblings ride with it.
COMPANIONS: dict[str, tuple[str, ...]] = {
    "deck.py": ("document.py", "extract.py"),
    "document.py": ("extract.py",),
    "spreadsheet.py": ("extract.py",),
}
SUITE_DIR = "/verify"  # neither /work nor /artifacts: the suite is not the agent's, and not a deliverable
# Where a suite leaves a binary's text for the judge; the job names it (a device frame's roots differ).
EXTRACT_FRAME_PATH = f"{WORK}/extracted.md"
EXTRACT_CHARS = 60_000  # a suite cuts its extract here and appends EXTRACT_CUT; the job carries both
EXTRACT_CUT = "\n…(truncated)"
PAGED: Final = (".docx", ".pdf", ".pptx")  # the files a person opens as pages
PAGES_FRAME_PATH = f"{WORK}/pages"  # where the house renderer leaves its PNGs and `pages.json`
PAGES_TIMEOUT_S = 330.0  # above one LibreOffice conversion (the renderer's own 240 s) plus rasterizing
MAX_PAGE_IMAGES = 12  # the renderer's sample cap, restated where the images are read
# The house renderer's entry, run as the system's own script: the frame's `plane_house` does the work.
_RENDER = b"import sys\nfrom plane_house.render import main\nsys.exit(main(sys.argv[1:]))\n"

# The suite scripts, by name. A suite the registry does not know cannot be run - same shape as the
# artifact-type registry at the publish door, and for the same reason.
SCRIPTS: dict[VerifierSuite, str] = {
    VerifierSuite.DOCUMENT: "document.py",
    VerifierSuite.SPREADSHEET: "spreadsheet.py",
    VerifierSuite.CODE: "code.py",
    VerifierSuite.DECK: "deck.py",
    VerifierSuite.DATASET: "dataset.py",
    VerifierSuite.COLLECTION: "collection.py",
    VerifierSuite.FLEET: "quotes.py",
    VerifierSuite.COUNCIL: "quotes.py",  # the fleet suite's pre-rename value: stored reports still name it
    VerifierSuite.RECORDING: "recording.py",
}


class SuiteNotBuilt(Exception):
    """Raised, never skipped: a type with no suite yields an artifact marked `unverified`, never a
    hollow pass."""


async def run_suite(
    frame: FrameDriver,
    suite: VerifierSuite,
    *,
    artifact_path: str,
    artifact_sha256: str,
    sources: list[str],
    allow_domains: list[str],
    read_pages: list[str] | None = None,
    timeout: float = 120.0,  # noqa: ASYNC109 - a deadline plumbed into the frame's exec, not an asyncio.timeout stand-in
    context: dict[str, object] | None = None,
    grounding: Grounding = GROUNDING,
) -> list[Check]:
    """Copy the suite into a fresh, populated frame, run it, parse its checks; `read_pages`, `context`
    (a caller-only fact on `job.json`) and `grounding` ride the job."""
    script = SCRIPTS.get(suite)
    if script is None:
        raise SuiteNotBuilt(f"no suite is built for {suite.value!r}")

    for companion in COMPANIONS.get(script, ()):
        await frame.write_bytes(
            f"{SUITE_DIR}/{companion}", (SUITES / companion).read_bytes(), mode=0o555, owner="system"
        )
    body = (SUITES / script).read_bytes()
    # `owner="system"`: the suite is not the agent's file, and in a fresh frame there is no agent -
    # but the frame driver's default is `agent`, and a verifier the subject could edit is not one.
    await frame.write_bytes(f"{SUITE_DIR}/{script}", body, mode=0o555, owner="system")

    job = json.dumps(
        {
            "artifact_path": frame.paths.render(artifact_path),
            "artifact_sha256": artifact_sha256,
            "sources": [frame.paths.render(source) for source in sources],
            "allow_domains": allow_domains,
            "read_pages": read_pages or [],
            "extract_path": frame.paths.render(EXTRACT_FRAME_PATH),
            "extract_chars": EXTRACT_CHARS,
            "extract_cut": EXTRACT_CUT,
            "context": context or {},
            "numbers": grounding is Grounding.NUMBERS,
        }
    )
    await frame.write_bytes(f"{SUITE_DIR}/job.json", job.encode(), mode=0o444, owner="system")

    # An absolute interpreter path. `PATH` is a thing a process can change, and "run the verifier"
    # is not a place to accept a substitution. (The device's is its own, `DeviceFrame.system_python`.)
    result = await run_python(
        frame,
        f"{SUITE_DIR}/{script}",
        stdin=f"{SUITE_DIR}/job.json",
        timeout=timeout,
        interpreter="/usr/local/bin/python3",
    )

    if not result.ok:
        # A suite that crashed did not pass. It did not fail the *document* either - it failed
        # itself, and saying which is the difference between "your memo is wrong" and "our verifier
        # is broken". Both are FAIL; only one is the author's problem.
        return [
            Check(
                name="suite:ran",
                status=CheckStatus.FAIL,
                kind=CheckKind.STRUCTURAL,
                detail=f"the {suite.value} exited {result.exit_code}",
                actual=(result.stderr or result.stdout)[-500:],
            )
        ]

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return [
            Check(
                name="suite:ran",
                status=CheckStatus.FAIL,
                kind=CheckKind.STRUCTURAL,
                detail=f"the {suite.value} did not emit JSON",
                actual=result.stdout[-500:],
            )
        ]

    checks = [Check.model_validate(c) for c in payload["checks"]]
    try:
        for check in checks:
            if check.name == TESTS_RUN and check.actual is not None:
                CodeReceipt.model_validate_json(check.actual)
    except ValidationError as bad:  # fail-closed: a receipt off its schema is a broken suite, never a pass
        return [
            Check(
                name="suite:ran",
                status=CheckStatus.FAIL,
                kind=CheckKind.STRUCTURAL,
                detail=f"the {suite.value} wrote a malformed test receipt",
                actual=str(bad)[-500:],
            )
        ]
    return checks


@dataclass(frozen=True, slots=True)
class PageImage:
    """One picture a judge is shown: a rendered page by its number, or a screenshot (None)."""

    number: int | None
    png: bytes


async def render_pages(frame: FrameDriver, artifact_path: str) -> tuple[PageImage, ...]:
    """The paged file's sampled pages, rendered in this frame. Nothing when it cannot render: the
    judge then reads the text alone (fail-soft: the pictures are an aid, the receipt stands without them)."""
    script = f"{SUITE_DIR}/render.py"
    try:
        await frame.write_bytes(script, _RENDER, mode=0o555, owner="system")
        path, pages = frame.paths.render(artifact_path), frame.paths.render(PAGES_FRAME_PATH)
        await run_python(frame, script, path, "--pages", pages, timeout=PAGES_TIMEOUT_S)
    except Exception as exc:
        logfire.info("pages not rendered ({kind})", kind=type(exc).__name__)
        return ()
    return await sampled_pages(frame)


async def sampled_pages(frame: FrameDriver) -> tuple[PageImage, ...]:
    """The pages the house renderer sampled into this frame, in page order, capped. No manifest or an
    unreadable image means fewer pages, never a failure (fail-soft, as `render_pages`)."""
    try:
        manifest = json.loads(await frame.read_file(f"{PAGES_FRAME_PATH}/pages.json"))
    except Exception as exc:
        logfire.info("no sampled page manifest ({kind})", kind=type(exc).__name__)
        return ()
    out: list[PageImage] = []
    for path in list(manifest.get("images") or [])[:MAX_PAGE_IMAGES]:
        try:
            number = int(str(path).rsplit("-", 1)[-1].split(".")[0])
            out.append(PageImage(number, await frame.read_bytes(str(path))))
        except Exception as exc:
            logfire.info("sampled page {path} unreadable ({kind})", path=str(path), kind=type(exc).__name__)
    return tuple(out)


def verdict_of(checks: list[Check]) -> Verdict:
    """FAIL if anything failed; PASS only if a substantive check (one that read the claims) passed;
    else UNVERIFIED, since skips or a hash prove nothing about the content."""
    if any(c.status is CheckStatus.FAIL for c in checks):
        return Verdict.FAIL
    if any(c.status is CheckStatus.PASS and c.kind is CheckKind.SUBSTANTIVE for c in checks):
        return Verdict.PASS
    return Verdict.UNVERIFIED


def final_verdict(checks: list[Check], judge: JudgeOpinion | None, *, judge_read_content: bool) -> Verdict:
    """Both halves: a deterministic FAIL is a floor nothing lifts, a judge FAIL sinks, and a judge
    PASS lifts UNVERIFIED only when the judge read the content and the code it read was not untested."""
    deterministic = verdict_of(checks)
    if deterministic is Verdict.FAIL:
        return Verdict.FAIL
    if judge is not None and judge.verdict is Verdict.FAIL:
        return Verdict.FAIL
    if (
        deterministic is Verdict.UNVERIFIED
        and judge is not None
        and judge.verdict is Verdict.PASS
        and judge_read_content
        and not untested(checks)
    ):
        return Verdict.PASS  # the judge examined what the suite could not
    return deterministic


def untested(checks: list[Check]) -> bool:
    """Whether the code suite found nothing to run: reading code is no stand-in for running its tests."""
    return any(c.name == TESTS_RUN and c.status is CheckStatus.SKIP for c in checks)


def artifact_frame_path(published_path: str) -> str:
    """Where the published bytes are put in the verifier's frame - the same place they lived in the
    producer's, so a suite never has to care which frame it is in."""
    name = published_path.rsplit("/", 1)[-1]
    return f"{ARTIFACTS}/{name}"
