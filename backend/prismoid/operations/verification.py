# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Verification - the operation that makes a green check mean something.

    fetch the artifact + the bytes it was made from
      → a FRESH frame, with no network and nothing of the agent's in it
      → the type's suite, run against the published bytes           (deterministic)
      → a judge seated as far from the producer as the host allows (judgement)
      → Ed25519 over the canonical report                           (non-repudiation)
      → a `Verification` row + `verification.signed` on the stream

`operations` is the only composer, and this function is why: the **engine** has a sandbox and
no database, the **database** has bytes and no sandbox, and somebody has to hold both. It is here,
and it is here only.

**Why a fresh frame, when the design says "in the frame":** the step's frame is territory the agent
controlled for the whole of its run, and it no longer exists by the time anyone types
`prismoid artifacts verify $AID`. Both facts point the same way. See `verifier/pipeline.py`.

**What the judge is shown** (`deliverable_view`): readable text, never raw bytes (a judge handed line
noise fails correct work). Text goes in as-is, code untarred, a docx, deck or workbook as the text its
suite extracted in the frame; all of it between fence lines with a fresh nonce nothing inside can
close. A placeholder is unreadable: a judge PASS over it never promotes, a FAIL still sinks (fail-safe).
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import re
import secrets
import tarfile
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any

import logfire
from pydantic_ai import BinaryContent
from pydantic_ai.messages import UserContent
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid import storage
from prismoid.ai.deps import TaskDeps
from prismoid.ai.frames import ensure_ready, new_frame
from prismoid.ai.frames.driver import FrameDriver
from prismoid.ai.frames.layout import input_path
from prismoid.ai.gateway.resolve import JudgeSeats, judge_seats
from prismoid.ai.skills import deliverables
from prismoid.ai.tools.labels import READ_TOOLS, page_read
from prismoid.ai.verification import change as change_suite
from prismoid.ai.verification import grounding, pipeline, signing
from prismoid.db.base import utcnow
from prismoid.db.models.artifact import Artifact
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.models.verification import Verification
from prismoid.errors import HostFull, NotFound, PrismoidError
from prismoid.operations import grounding as grounding_ops
from prismoid.operations import machines
from prismoid.operations import models as model_ops
from prismoid.operations import routing as routing_ops
from prismoid.operations import sources as sources_ops
from prismoid.operations import usage as usage_ops
from prismoid.operations.deadline import hard_timeout
from prismoid.schemas.artifacts import ManifestEntry
from prismoid.schemas.contract import Criteria
from prismoid.schemas.events import VerificationSigned
from prismoid.schemas.ids import ArtifactId, ProjectId, StepId, TaskId
from prismoid.schemas.inputs import InputSet, TaskInput
from prismoid.schemas.machines import FrameKind, MachineProfile, Segment, frame_labels
from prismoid.schemas.model_class import Family, JudgeFamily, ModelClass
from prismoid.schemas.outputs import ArtifactType, VerifierSuite, spec_for
from prismoid.schemas.plan import StepSpec
from prismoid.schemas.steering import merge_directives
from prismoid.schemas.usage import UsageRecord
from prismoid.schemas.verification import (
    REPORT_SCHEMA_VERSION,
    CheckKind,
    JudgeOpinion,
    JudgeVerdict,
    SignedVerification,
    Verdict,
    VerificationReport,
)

# The receipt judge's wall clock: a hosted judge answers in seconds, one silent this long is wedged. Past
# it the next seat is tried; a ceiling on waiting, never a way to turn a FAIL into a pass.
JUDGE_TIMEOUT_S = 90.0


_RECORDING_ATTEMPT = re.compile(r"(?:^|/)recording-pub-(\d+)-[0-9a-f]+(?:\.tar)?$")


_FRAME_RETRY_S = 30.0  # a full host's room comes back when a computer sleeps, not in milliseconds
_NO_FRAME = "suite:frame"  # the one check a receipt carries when no frame ever came


def recording_attempt(path: str) -> int | None:
    """The step attempt a recording was published from - named in its publish path by the
    engine (`_publish_recording`: `recording-pub-{attempt}-{nonce}`). None for any other path."""
    hit = _RECORDING_ATTEMPT.search(path or "")
    return int(hit.group(1)) if hit else None


async def verify_artifact(
    session: AsyncSession, artifact: Artifact, *, frame_wait_s: float | None = None
) -> SignedVerification:
    """Run the suite for this artifact's type and record the report. The one entry point.

    `frame_wait_s` is how long a full host may hold the receipt: with it, a frame that
    never comes signs UNVERIFIED, saying why, instead of a Pending nobody ends. The in-line fleet
    leaves it None, so a full host raises there as any failed frame does."""
    task = await session.get(Task, artifact.task_id)
    if task is None:
        raise NotFound("no such task")

    provenance = ManifestEntry.model_validate(artifact.provenance)
    kind = spec_for(ArtifactType(artifact.type))
    suite = kind.verifier_suite
    producer_family = await author_family(session, provenance.model)

    payload = await storage.aget(artifact.sha256)
    sources = await _sources_for(session, task, provenance)
    allow_domains = await _allow_domains(session, artifact.step_id)
    read_pages = await _read_pages(session, task.id)
    # The pages the task kept (`operations/sources.py`): a web quote or figure is checked against
    # the page that was read. The suite's alone; the judge reads the declared sources.
    pages = await sources_ops.pages(session, task.id)
    kept = await grounding_ops.kept(session, task.id)  # read before the long awaits (the suite, the models)

    # **The yardstick is the producing step's contract, not the raw task goal.** A multi-step
    # plan's interim and structural artifacts (a fleet's table step, a researcher's findings)
    # must be judged against what THEIR step was asked to do; the task's phrasing belongs to the
    # steps that were asked to answer it. Tasks without a step contract fall back to task criteria.
    goal, musts = task.goal, list(Criteria.model_validate(task.criteria or {}).must)
    if artifact.step_id and (step := await session.get(PlanStep, artifact.step_id)):
        spec = StepSpec.model_validate(step.spec)
        if spec.contract:
            from prismoid.operations import steering

            # the receipt checks the steers the step took, as criteria (never the goal).
            taken = [row.text for row in await steering.seen_by(session, task, step)]
            goal, musts = merge_directives(spec.contract.goal, list(spec.contract.acceptance), taken)

    context: dict[str, object] | None = None
    if suite is VerifierSuite.RECORDING and artifact.step_id is not None:
        from prismoid.schemas.computer_use import RECORDED_VERBS

        # ONE attempt's calls against that attempt's recording: a retried step journaled its
        # earlier attempts too. The artifact row has no attempt column; the engine's publish
        # path names it (`recording-pub-{attempt}-{nonce}`), and a path that does not (an older
        # row) falls back to the step's LAST journaled attempt.
        attempt = recording_attempt(provenance.path)
        if attempt is None:
            attempt = await TaskEvent.last_tool_attempt(
                session, task_id=artifact.task_id, step_id=artifact.step_id
            )
        n = await TaskEvent.count_tool_calls(
            session,
            task_id=artifact.task_id,
            step_id=artifact.step_id,
            prefix="browser.",
            verbs=RECORDED_VERBS,
            attempt=attempt,
        )
        context = {"expected_actions": n}

    # The seats first: the pages are rendered only when one of them can see them.
    seats = await judge_seats_for(session, provenance.model, producer_family) if kind.judgeable else None
    sighted = seats is not None and any(row.serves(ModelClass.VISION) for row in seats.rows)
    frame_seconds: list[int] = []
    ran = await _run_suite(
        suite=suite,
        artifact_name=provenance.path,
        payload=payload,
        sha256=artifact.sha256,
        sources={**sources, **pages},
        allow_domains=allow_domains,
        read_pages=read_pages,
        context=context,
        labels=frame_labels(FrameKind.VERIFY, workspace_id=task.workspace_id, task_id=task.id),
        timed=frame_seconds.append,
        frame_wait_s=frame_wait_s,
        pages=sighted and provenance.path.lower().endswith(pipeline.PAGED),
    )
    checks = ran.checks + await grounding_ops.ground(
        session, artifact, suite, ran.extracted, files=sources, pages=kept
    )
    if ran.preview is not None:
        # The render sidecar: its own CAS blob and a nullable pointer - NEVER part
        # of the signed report below. Presentation is replaceable; attestation is not.
        artifact.preview_sha256 = await storage.aput(ran.preview)
    view = deliverable_view(artifact, payload, sources, ran.extracted)

    # No frame, no suite: `final_verdict` lets a judge speak for a suite with nothing to examine, and
    # this one had plenty, so a PASS here would sign what no check read.
    suite_ran = not any(c.name == _NO_FRAME for c in checks)
    opinion = (
        await judge(
            session,
            task,
            artifact,
            seats=seats,
            view=view,
            sources=sources,
            pages=kept,
            checks=checks,
            images=_screenshots_of(artifact, payload) or ran.pages,
            goal=goal,
            musts=musts,
        )
        if seats is not None and suite_ran
        else None  # a judge that cannot open the work never decides its receipt
    )

    report = VerificationReport(
        artifact_sha256=artifact.sha256,
        suite=suite,
        checks=checks,
        judge=opinion,
        verdict=pipeline.final_verdict(checks, opinion, judge_read_content=view.readable),
        producer_model=provenance.model,
        producer_family=producer_family,
        attempt=1,
        at=utcnow(),
        schema_version=REPORT_SCHEMA_VERSION,
    )

    # Signed over the whole report, so a quiet edit in psql reads `signature_valid: false` (the tamper test).
    signed = signing.sign(report)

    await _record(session, task, artifact, signed)
    # The verify frame's warm time, on the CALLER's session (the in-line fleet's may hold the task's row)
    # and only here: billed when the frame went, it would hold that row across the judge's calls.
    for seconds in frame_seconds:
        await machines.bill(session, Segment(task.id, MachineProfile.BASE, seconds))
    return signed


@dataclass(frozen=True)
class Judged:
    """A judge's answer, and which model gave it: the ledger's word, never the model's."""

    verdict: JudgeVerdict
    model: str
    family: Family


async def judge_seats_for(session: AsyncSession, maker_model: str, maker_family: Family) -> JudgeSeats:
    """The judge rows for this maker's work, furthest from the maker first (`resolve.judge_seats`)."""
    catalog, order = await model_ops.routed_catalog(session)
    asked = (await routing_ops.settings_for(session)).get(ModelClass.JUDGE.value)
    return judge_seats(
        catalog, maker_family=maker_family, maker_model=maker_model, order=order, settings=asked
    )


async def ask_judge(
    seats: JudgeSeats, deps: TaskDeps, prompt: str, images: tuple[pipeline.PageImage, ...], *, seconds: float
) -> Judged | None:
    """Each judge seat in turn until one answers; None when none can be built or answered, and the
    deterministic verdict stands alone (fail-soft: losing the judge means unverified, never failed)."""
    from prismoid.ai.agents import base as agents

    if seats.family is JudgeFamily.NONE:
        logfire.warn("no judge row can be built on this host - the report will say so")
        return None
    seen: list[UsageRecord] = []
    metered = deps.emit_usage

    async def capture(record: UsageRecord) -> None:
        seen.append(record)
        if metered is not None:
            await metered(record)

    deps = replace(deps, emit_usage=capture)
    for spec in seats.rows:
        deps.deadline = time.monotonic() + seconds  # its cited-page reads stop inside this seat's wall
        try:
            runner = agents.build("judge", deps, [spec], settings={ModelClass.JUDGE.value: seats.settings})
            asked = _with_images(prompt, images, spec.serves(ModelClass.VISION))
            result = await hard_timeout(runner.run(asked), seconds)
        except (
            Exception
        ) as failed:  # a 429, a timeout, an incapable model: the next seat (never CancelledError)
            logfire.warn(
                "judge {model} did not answer ({kind}: {why}) - trying the next seat",
                model=spec.ref,
                kind=type(failed).__name__,
                why=str(failed)[:160],
            )
            continue
        verdict = result.output
        if not isinstance(verdict, JudgeVerdict):
            raise PrismoidError(f"judge {spec.ref} answered a {type(verdict).__name__}, not a verdict")
        if not seen:  # pragma: no cover - every provider call meters
            return None
        return Judged(verdict, f"{seen[-1].provider}:{seen[-1].model}", seen[-1].family)
    logfire.warn("every {judges} judge candidate failed to produce a verdict", judges=seats.family.value)
    return None


async def judge(
    session: AsyncSession,
    task: Task,
    artifact: Artifact,
    *,
    seats: JudgeSeats,
    view: DeliverableView,
    sources: dict[str, bytes],
    pages: Sequence[grounding.Source],
    checks: list[pipeline.Check],
    images: tuple[pipeline.PageImage, ...],
    goal: str,
    musts: list[str],
) -> JudgeOpinion | None:
    """The judge reads the work in a fresh context: only the rubric and the work, never the producing
    conversation; None when nobody answered."""
    deps = TaskDeps(
        task_id=TaskId(task.id),
        step_id=StepId(artifact.step_id) if artifact.step_id else None,
        project_id=ProjectId(task.project_id) if task.project_id else None,
        frame=None,  # the judge gets the bytes, in the prompt, and nothing else
        emit_usage=usage_ops.emitter(session, own_transaction=True),  # never the caller's connection
    )
    prompt = _judge_prompt(task, artifact, view, sources, checks, goal=goal, musts=musts, pages=pages)
    judged = await ask_judge(seats, deps, prompt, images, seconds=JUDGE_TIMEOUT_S)
    if judged is None:
        return None
    return JudgeOpinion(
        verdict=judged.verdict.verdict,
        rationale=judged.verdict.rationale,
        model=judged.model,
        family=judged.family,
        criteria_met=judged.verdict.criteria_met,
        criteria_missed=judged.verdict.criteria_missed,
        bad_derivations=judged.verdict.bad_derivations,
    )


def _judge_prompt(
    task: Task,
    artifact: Artifact,
    view: DeliverableView,
    sources: dict[str, bytes],
    checks: list[pipeline.Check],
    *,
    goal: str,
    musts: list[str],
    pages: Sequence[grounding.Source] = (),
) -> str:
    """Everything the judge needs and nothing it could be steered by: the deterministic findings verbatim,
    judged against the producing step's own `goal`/`musts`, and the sources: the declared files, then each
    page the deliverable cites under its source id, within one bound (`grounding.judged_sources`)."""
    is_change = artifact.type == ArtifactType.CHANGE.value
    lines = [
        "## What this deliverable was asked to do",
        goal,
        "",
        "## What 'done' means",
        *(f"- {c}" for c in (musts or ["(no criteria were recorded)"])),
        "",
        "## The deterministic suite already found this",
    ]
    for check in checks:
        lines.append(f"- [{check.status.value}] {check.name}: {check.detail}")
        if check.actual and not is_change:  # a change's `actual` holds names the agent chose
            lines.append(f"      → {check.actual}")

    # A change's title and sources ride after its law, fenced (`change_suite.evidence`): a follow-up's
    # own artifact joins here by name, and the agent's words above the law would read as criteria.
    if is_change:
        lines += ["", "## The deliverable - a change", view.text]
        return "\n".join(lines)

    lines += ["", "## The sources - everything the author was given"]
    for name, data in sources.items():
        lines.append(_as_data("a source the author was given", f"{name}\n{_clip(data)}"))
    lines += grounding.judged_sources(pages, view.text, secrets.token_hex(8))
    if not sources and not pages:
        lines.append("(none - the author was given nothing, so every claim is unsourced by construction)")

    lines += ["", f"## The deliverable - {artifact.title}", view.text]
    return "\n".join(lines)


def _screenshots_of(artifact: Artifact, payload: bytes) -> tuple[pipeline.PageImage, ...]:
    """What a change shows the judge beside its text; nothing for any other type."""
    if artifact.type != ArtifactType.CHANGE.value:
        return ()
    change = change_suite.read_change(payload)
    return (
        tuple(pipeline.PageImage(None, shot) for shot in change_suite.judged_screenshots(change))
        if change
        else ()
    )


def _with_images(
    prompt: str, images: tuple[pipeline.PageImage, ...], sees_images: bool
) -> str | list[UserContent]:
    """The judge's prompt, with its pictures where its model can open them, each page by its number.
    One that cannot see is told, so it never takes a promised picture for one it saw."""
    if not images:
        return prompt
    if not sees_images:
        return f"{prompt}\n\nThe images could not be shown to you: judge what the text supports."
    content: list[UserContent] = [prompt]
    for image in images:
        if image.number is not None:
            content.append(f"[page {image.number}]")
        content.append(BinaryContent(data=image.png, media_type="image/png"))
    return content


@dataclass(frozen=True)
class DeliverableView:
    """What the judge is shown of the deliverable, and whether that is its content (a placeholder is not)."""

    text: str
    readable: bool = True


def deliverable_view(
    artifact: Artifact,
    payload: bytes,
    sources: Mapping[str, bytes] = MappingProxyType({}),
    extracted: str | None = None,
) -> DeliverableView:
    """What the judge is shown of the deliverable: readable text, never raw bytes, the author's words
    fenced as data; a placeholder is marked unreadable (module docstring)."""
    from prismoid.operations.artifacts import filename as artifact_filename
    from prismoid.schemas.outputs import ArtifactType, is_text_path, spec_for

    type_ = ArtifactType(artifact.type)
    if type_ is ArtifactType.CHANGE:
        change = change_suite.read_change(payload)
        if change is None:
            return DeliverableView("*(the change could not be read)*", readable=False)
        # The change's own bytes are not a source of itself: a follow-up's artifact joins `sources` by name.
        kept = {n: d for n, d in sources.items() if hashlib.sha256(d).hexdigest() != artifact.sha256}
        return DeliverableView(change_suite.evidence(change, artifact.title, kept))
    # Text-ness is the FILE's, not the type's: a `document` can be a `.docx`, and `code` is a tar.
    if spec_for(type_).streams_preview and is_text_path(artifact_filename(artifact)):
        return DeliverableView(_as_data(f"the {type_.value} file", _clip(payload)))
    if type_ in (ArtifactType.CODE, ArtifactType.WEBAPP):  # an App is the same tar, honestly
        return code_view(payload)
    if extracted is not None:
        return DeliverableView(
            _as_data(f"the text the suite extracted from the {type_.value} file", _clip(extracted))
        )
    return DeliverableView(
        f"*(a binary `{type_.value}` artifact, {len(payload):,} bytes - you cannot read its raw bytes. "
        "Judge whether it meets the task's criteria from the deterministic suite's findings above and "
        "the task goal; the suite has already re-derived and checked its contents.)*",
        readable=False,
    )


# What the judge reads of a code archive: so many files, so much of each, so much in all.
CODE_VIEW_FILES = 200
CODE_VIEW_FILE_CHARS = 8_000
CODE_VIEW_CHARS = 40_000


def code_view(payload: bytes) -> DeliverableView:
    """A code artifact is a tar of its project; show the judge the source, file by file.

    Bounded before it is read, because whoever ran the task chose the bytes and this runs in the
    shared worker: `r:` opens only a plain tar (publish never compresses, and a compressed member
    stream inflates past any cap), each file is read only to its slice, and the walk stops at
    `CODE_VIEW_FILES` files or `CODE_VIEW_CHARS` characters."""
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as tar:
            parts = _code_files(tar)
    except (tarfile.TarError, OSError, EOFError, ValueError):
        return DeliverableView("*(the code archive could not be read)*", readable=False)
    if not parts:
        return DeliverableView("*(the code archive was empty)*", readable=False)
    return DeliverableView(_as_data("the code archive", "\n\n".join(parts)[:CODE_VIEW_CHARS]))


def _code_files(tar: tarfile.TarFile) -> list[str]:
    parts: list[str] = []
    total = 0
    for member in tar:
        if len(parts) >= CODE_VIEW_FILES or total >= CODE_VIEW_CHARS:
            break
        extracted = tar.extractfile(member) if member.isfile() else None
        if extracted is None:
            continue
        text = extracted.read(CODE_VIEW_FILE_CHARS).decode("utf-8", errors="replace")
        parts.append(f"### {member.name}\n```\n{text[:CODE_VIEW_FILE_CHARS]}\n```")
        total += len(parts[-1])
    return parts


def _as_data(name: str, body: str) -> str:
    """The author's `body` between fence lines it cannot close: a nonce per call, the mark broken up inside."""
    return grounding.judge_fence(name, body, secrets.token_hex(8))


def _clip(data: bytes | str, limit: int = 40_000) -> str:
    """Long text is truncated, and the truncation **says so**, never misstating how long the text was."""
    text = data if isinstance(data, str) else data.decode("utf-8", errors="replace")
    if len(text) <= limit:
        return text
    cut = text.endswith(pipeline.EXTRACT_CUT)  # the suite already cut it: its real length is unknown
    whole = f"more than {len(text) - len(pipeline.EXTRACT_CUT):,}" if cut else f"{len(text):,}"
    return text[:limit] + f"\n\n[… truncated at {limit:,} characters of {whole} - the tail was NOT read]"


PREVIEW_FRAME_PATH = "/work/preview.json"  # where a suite MAY leave a render sidecar (M4)
PREVIEW_CEILING = 128_000  # defensive: twice the suite's own cap; past this it is not a preview
# The suite's cap in characters at four bytes each, with its cut line; past this it is not ours.
EXTRACT_CEILING = 4 * (pipeline.EXTRACT_CHARS + len(pipeline.EXTRACT_CUT))


@dataclass(frozen=True)
class SuiteRun:
    """What one suite run leaves: its checks, the render sidecar, and the text it read out of a binary."""

    checks: list[pipeline.Check]
    preview: bytes | None = None
    extracted: str | None = None
    pages: tuple[pipeline.PageImage, ...] = ()


async def _run_suite(*, suite: pipeline.VerifierSuite, payload: bytes, **frame_args: Any) -> SuiteRun:
    """The suite's checks and what it left. A change opens no frame: it is read here, on
    a thread because it decodes pictures, and nothing is ever run for it (`verifier/change.py`)."""
    if suite is VerifierSuite.CHANGE:
        return SuiteRun(await asyncio.to_thread(change_suite.check_change, payload))
    return await run_in_a_fresh_frame(suite=suite, payload=payload, **frame_args)


async def _sidecar(frame: FrameDriver, path: str, ceiling: int, what: str) -> bytes | None:
    """A file a suite MAY leave in /work. Absent, empty or past `ceiling` is None, never a failed receipt."""
    try:
        data = await frame.read_bytes(path)
    except Exception as exc:
        logfire.info("verify: no {what} read from the frame ({kind})", what=what, kind=type(exc).__name__)
        return None
    return data if 0 < len(data) <= ceiling else None


async def run_in_a_fresh_frame(
    *,
    suite: pipeline.VerifierSuite,
    artifact_name: str,
    payload: bytes,
    sha256: str,
    sources: dict[str, bytes],
    allow_domains: list[str],
    read_pages: list[str] | None = None,
    context: dict[str, object] | None = None,
    labels: dict[str, str] | None = None,
    timed: Callable[[int], object] | None = None,
    frame_wait_s: float | None = None,
    pages: bool = False,
) -> SuiteRun:
    """The suite in a clean sandbox with the published and source bytes only; its sidecars (and `pages`)
    are read before the frame goes."""
    profile = deliverables.verify_profile(suite)
    ensure_ready(profile)
    # A verifier needs no network. It is checking, not fetching.
    frame_labelled = {**frame_labels(FrameKind.VERIFY), **(labels or {})}
    deadline = time.monotonic() + (frame_wait_s or 0)
    while True:
        frame = new_frame(task_id=f"verify-{sha256[:12]}", profile=profile)
        try:
            await frame.create(allow_domains=[], labels=frame_labelled)
            break
        except HostFull as full:
            left = deadline - time.monotonic()
            if frame_wait_s is None:
                raise
            if left <= 0:
                # `suite:*` is structural and no judge reads it, so this signs UNVERIFIED: an honest ending.
                return SuiteRun(
                    [
                        pipeline.Check(
                            name=_NO_FRAME,
                            status=pipeline.CheckStatus.SKIP,
                            kind=CheckKind.STRUCTURAL,
                            detail=f"no computer was free to run the checks in ({full})",
                        )
                    ]
                )
            await asyncio.sleep(min(_FRAME_RETRY_S, left))
    started = time.monotonic()  # the frame's clock starts once it is up, as every frame's
    try:
        artifact_path = pipeline.artifact_frame_path(artifact_name)
        await frame.write_bytes(artifact_path, payload, mode=0o444, owner="system")

        source_paths: list[str] = []
        for filename, data in sources.items():
            path = input_path(filename)
            await frame.write_bytes(path, data, mode=0o444, owner="system")
            source_paths.append(path)

        try:
            checks = await pipeline.run_suite(
                frame,
                suite,
                artifact_path=artifact_path,
                artifact_sha256=sha256,
                sources=source_paths,
                allow_domains=allow_domains,
                read_pages=read_pages,
                context=context,
            )
        except pipeline.SuiteNotBuilt as gap:
            # Our gap, not the author's error: a structural SKIP signs UNVERIFIED, never a FAIL.
            return SuiteRun(
                [
                    pipeline.Check(
                        name="suite:built",
                        status=pipeline.CheckStatus.SKIP,
                        kind=CheckKind.STRUCTURAL,
                        detail=f"{gap} - the artifact ships unverified until that suite lands (ADR-015)",
                    )
                ]
            )

        preview = await _sidecar(frame, PREVIEW_FRAME_PATH, PREVIEW_CEILING, "preview")
        extract = await _sidecar(frame, pipeline.EXTRACT_FRAME_PATH, EXTRACT_CEILING, "extracted text")
        rendered = await pipeline.render_pages(frame, artifact_path) if pages else ()
        text = extract.decode("utf-8", errors="replace") if extract else None
        return SuiteRun(checks, preview, text, rendered)
    finally:
        await frame.destroy()
        if timed is not None:
            timed(int(time.monotonic() - started))


async def _sources_for(session: AsyncSession, task: Task, provenance: ManifestEntry) -> dict[str, bytes]:
    """The bytes the artifact claims to derive from, matched by filename against the task's inputs, its
    own artifacts and its parent's; a source the task never had is dropped, loudly."""
    from prismoid.operations.execution import seat_filename

    by_input = {
        i.filename: i.sha256 for i in InputSet(items=[TaskInput.model_validate(i) for i in task.inputs]).items
    }
    artifacts = await Artifact.for_task(session, task.id)
    by_artifact = {
        str(a.provenance.get("path", "")).rsplit("/", 1)[-1]: a.sha256
        for a in artifacts
        if a.provenance.get("path")
    }
    # **The fleet table step's sources are seat MOUNT names (`answer-<model>.md`), not the answers' own
    # published names**. The executor renamed them at mount time; reverse it here with the
    # one shared derivation, or the table's sources resolve to nothing and it can never verify.
    by_seat = {
        seat_filename(str(a.provenance.get("model"))): a.sha256
        for a in artifacts
        if a.provenance.get("model")
    }

    # **A follow-up's sources include the parent's deliverables**. The executor mounts them
    # into `/task/inputs/` under their published names; without this join, an inherited source
    # resolved to nothing, the warning below fired for legitimate provenance, and a follow-up's
    # deliverable could never verify against the very document it was asked to build on.
    # Fetched lazily: most artifacts resolve every source locally, and verification runs once
    # per artifact - the parent query should cost only the artifacts that actually inherit.
    by_parent: dict[str, str] | None = None

    out: dict[str, bytes] = {}
    for path in provenance.sources:
        name = path.rsplit("/", 1)[-1]
        sha = by_input.get(name) or by_artifact.get(name) or by_seat.get(name)
        if sha is None and by_parent is None and task.parent_task_id is not None:
            by_parent = {
                str(a.provenance.get("path", "")).rsplit("/", 1)[-1]: a.sha256
                for a in await Artifact.for_task(session, task.parent_task_id)
                if a.provenance.get("path")
            }
        if sha is None and by_parent is not None:
            sha = by_parent.get(name)
        if sha is None:
            logfire.warn("artifact claims a source the task never had: {path}", path=path)
            continue
        out[name] = await storage.aget(sha)
    return out


async def _allow_domains(session: AsyncSession, step_id: uuid.UUID | None) -> list[str]:
    """The producing step's egress allowlist: the hosts its frame's proxy could reach.

    A frame has no route out but that proxy, so a link to a host on this list is one the step could
    have read. It is not the whole of what a task reads: the web door and the browser's grants
    never pass through this list, and `_read_pages` holds the pages those read.
    """
    if step_id is None:
        return []
    step = await session.get(PlanStep, step_id)
    if step is None:
        return []
    spec = StepSpec.model_validate(step.spec)
    return list(spec.contract.allow_domains) if spec.contract else []


async def _read_pages(session: AsyncSession, task_id: uuid.UUID) -> list[str]:
    """Every page this task read, host+path as its journal spells them: each `ok` `web.fetch` and
    each page the browser loaded (`READ_TOOLS`), from any step and any turn, for
    `links:resolve`. `web.fetch` runs in the worker and a browser grant widens a Machine, not a
    contract, so neither page is ever on `_allow_domains`. A failed call read nothing."""
    calls = await TaskEvent.ok_tool_summaries(session, task_id=task_id, tools=READ_TOOLS)
    return sorted({page for tool, summary in calls if (page := page_read(tool, summary))})


async def _record(session: AsyncSession, task: Task, artifact: Artifact, signed: SignedVerification) -> None:
    report = signed.report
    session.add(
        Verification(
            id=uuid.uuid4(),
            artifact_id=artifact.id,
            verdict=report.verdict.value,
            suite=report.suite.value,
            producer_family=str(report.producer_family),
            verifier_family=str(report.judge.family) if report.judge else None,
            report=report.model_dump(mode="json"),
            signature=signed.signature,
            public_key=signed.public_key,
            algorithm=signed.algorithm,
        )
    )

    seq = await task.next_seq(session)
    await TaskEvent.append(
        session,
        VerificationSigned(
            seq=seq,
            task_id=TaskId(task.id),
            step_id=StepId(artifact.step_id) if artifact.step_id else None,
            project_id=ProjectId(task.project_id) if task.project_id else None,
            at=utcnow(),
            artifact_id=ArtifactId(artifact.id),
            verdict=report.verdict,
            suite=report.suite,
            verifier_family=report.judge.family if report.judge else None,
            signature=signed.signature,
        ),
    )
    logfire.info(
        "verified {artifact} → {verdict} ({checks} checks)",
        artifact=str(artifact.id),
        verdict=report.verdict.value,
        checks=len(report.checks),
    )


async def latest_for(session: AsyncSession, artifact_id: uuid.UUID) -> SignedVerification | None:
    """The stored report, as a value. `None` means it has not been verified - which is not the same
    as `unverified`, and the API must not conflate them."""
    row = await Verification.latest_for(session, artifact_id)
    if row is None:
        return None
    return SignedVerification(
        report=VerificationReport.stored(row.report),
        signature=row.signature,
        public_key=row.public_key,
        algorithm=row.algorithm,
    )


async def verdict_for(session: AsyncSession, artifact_id: uuid.UUID) -> Verdict | None:
    row = await Verification.latest_for(session, artifact_id)
    return Verdict(row.verdict) if row else None


def signature_valid(signed: SignedVerification) -> bool | None:
    """`None` if it was never signed (a gap); otherwise `True`/`False`, **recomputed from the stored
    bytes** (`False` is someone having edited the row)."""
    if not signed.signature:
        return None
    return signing.is_valid(signed)


async def author_family(session: AsyncSession, model_ref: str) -> Family:
    """The producing model's FAMILY, from its catalog row (disabled rows count: a model switched off
    is still its work's author). The host is not the family: Baseten serves Zhipu's GLM."""
    for spec in await model_ops.catalog(session, enabled_only=False):
        if spec.ref == model_ref:
            return spec.family
    return _family_of(model_ref)


def _family_of(model_ref: str) -> Family:
    """The fallback for a ref no catalog row knows any more: its provider, the best guess left."""
    return Family(model_ref.split(":", 1)[0] if model_ref else "unknown")
