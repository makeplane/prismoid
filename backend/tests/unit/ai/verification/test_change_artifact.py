# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The `change` deliverable: what a coding task on the device publishes,
and the suite that judges it without ever touching the user's folder. Everything in the tar is the
agent's, so the reads are bounded and the bytes reach the judge as data. This file is the suite's own
checks: the tar, the report, the patch and the screenshots. The rig is `tests/support/rigs/change_rig.py`."""

from __future__ import annotations

import io
import struct
import tarfile
from typing import Any

import pytest

from prismoid.ai.frames.publish import validate_publish
from prismoid.ai.verification import change, pipeline
from prismoid.errors import PolicyDenied
from prismoid.operations import verification
from prismoid.schemas.outputs import (
    ARTIFACT_TYPES,
    ArtifactType,
    RenderHint,
    VerifierSuite,
    accepts,
    publish_types_doc,
    render_for,
)
from prismoid.schemas.verification import CheckStatus, Verdict
from tests.support.fakes.fake_frames import FakeFrame
from tests.support.rigs.change_rig import NO_GIT_REPORT, PATCH, REPORT, bundle, by_name, png, status


def test_change_accepts_directory() -> None:
    spec = ARTIFACT_TYPES[ArtifactType.CHANGE]
    assert spec.extensions == () and spec.verifier_suite is VerifierSuite.CHANGE
    assert ArtifactType.CHANGE.value == "change" and VerifierSuite.CHANGE.value == "change-suite"
    assert accepts(ArtifactType.CHANGE, "/out/change")
    assert not accepts(ArtifactType.CHANGE, "/out/change.tar")
    assert validate_publish("change", "/out/change", on_host=True) is ArtifactType.CHANGE
    assert render_for(ArtifactType.CHANGE, "change") is RenderHint.CHANGE


def test_a_cloud_frame_still_does_not_know_the_type() -> None:
    """The fresh-folder + `test.sh` contract stays for the cloud drivers: to them the
    word is what it was yesterday, an unknown type, in the same sentence."""
    with pytest.raises(PolicyDenied) as refused:
        validate_publish("change", "/work/change")
    assert str(refused.value).startswith("unknown artifact type 'change'. Known: ['document'")
    assert "'change'" not in str(refused.value).split("Known: ")[1]
    assert "change" not in publish_types_doc()
    assert "**change** (a directory)" in publish_types_doc(on_host=True)


def test_a_whole_change_passes_every_check_and_proves_nothing_alone() -> None:
    checks = change.check_change(bundle())
    assert {c.status for c in checks} == {CheckStatus.PASS}
    # Well-formed is not right: with no judge the receipt says so.
    assert pipeline.final_verdict(checks, None, judge_read_content=True) is Verdict.UNVERIFIED


@pytest.mark.parametrize(
    ("over", "check", "words"),
    [
        ({"report": None}, "format:report", "report.md"),
        ({"report": b"  \n"}, "format:report", "report.md"),
        ({"patch": None}, "format:patch", "patch.diff"),
        ({"patch": b"this is not a diff\n"}, "format:patch", "patch.diff"),
        ({"patch": b""}, "format:patch", "patch.diff"),
        ({"tests": None}, "format:test-output", "test-output.txt"),
        ({"tests": b""}, "format:test-output", "test-output.txt"),
        ({"shots": {"bad.png": b"\x89PNG\r\n\x1a\nnope"}}, "format:screenshots", "screenshots"),
        ({"shots": {"a.jpg": b"\xff\xd8\xff"}}, "format:screenshots", "screenshots"),
    ],
)
def test_change_suite_deterministic_checks(over: dict[str, Any], check: str, words: str) -> None:
    checks = {c.name: c for c in change.check_change(bundle(**over))}
    assert checks[check].status is CheckStatus.FAIL
    assert words in checks[check].detail
    others = {n: c.status for n, c in checks.items() if n != check}
    assert CheckStatus.FAIL not in others.values(), others


def test_not_a_tar_is_a_fail_not_a_crash() -> None:
    checks = change.check_change(b"not a tar at all" * 100)
    assert [(c.name, c.status) for c in checks] == [("format:tar", CheckStatus.FAIL)]


def test_a_compressed_archive_is_never_opened() -> None:
    """Publish writes a plain tar. A gzip one would be a decompression bomb past every cap."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo("change/report.md")
        info.size = 2
        tf.addfile(info, io.BytesIO(b"hi"))
    assert change.check_change(buf.getvalue())[0].status is CheckStatus.FAIL


@pytest.mark.parametrize("name", ["/report.md", "../report.md", "change/../../report.md"])
def test_a_member_that_climbs_is_ignored(name: str) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        info = tarfile.TarInfo(name)
        info.size = len(REPORT)
        tf.addfile(info, io.BytesIO(REPORT.encode()))
    assert status(buf.getvalue(), "format:report") is CheckStatus.FAIL


def test_a_link_member_is_not_a_file() -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        info = tarfile.TarInfo("change/report.md")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tf.addfile(info)
    assert status(buf.getvalue(), "format:report") is CheckStatus.FAIL


def test_a_hostile_size_field_is_a_fail_not_a_crash() -> None:
    """The crash left `check_change`, and the job swallowing it left the artifact Pending for good,
    past its FAIL floor."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
        info = tarfile.TarInfo("change/report.md")
        info.pax_headers = {"size": "9" * 24}
        tf.addfile(info)
    assert change.check_change(buf.getvalue())[0].status is CheckStatus.FAIL


def test_a_screenshot_nothing_could_read_is_not_a_pass() -> None:
    too_big = {"huge.png": b"x" * (change.MAX_SCREENSHOT_BYTES + 1)}
    assert status(bundle(shots=too_big), "format:screenshots") is CheckStatus.SKIP


@pytest.mark.parametrize(
    "claim",
    [
        "We made no git commits.",
        "No git push, as asked.",
        "no git changes were needed",
        "No git here, so no patch.",
        "There is no git.",
    ],
)
def test_no_git_is_a_line_of_its_own(claim: str) -> None:
    report = f"{claim}\n- src/app.py\n".encode()
    assert status(bundle(patch=None, report=report), "format:patch") is CheckStatus.FAIL


@pytest.mark.parametrize(
    "claim",
    [
        "no git",
        "No Git",
        "NO GIT.",
        "**No git**",
        "`no git`",
        "## No git",
        "- no git",
        '"no git"',
        "_no git!_",
    ],
)
def test_the_claim_may_wear_markdown_and_punctuation(claim: str) -> None:
    report = f"{claim}\n\n- src/app.py\n".encode()
    assert status(bundle(patch=None, report=report), "format:patch") is CheckStatus.PASS


@pytest.mark.parametrize(
    "item",
    [
        "- /work/app.py",
        "- `/work/src/app.py`",
        "- ./app.py",
        "- .github/workflows/ci.yml",
        "- **src/app.py**",
        "- app.py.",
        "- app.py;",
        "- src/app.py: added the route",
        "1. src/app.py",
        "* Makefile.am",
    ],
)
def test_a_listed_file_is_spelled_as_agents_spell_it(item: str) -> None:
    report = f"no git\n{item}\n".encode()
    assert status(bundle(patch=None, report=report), "format:patch") is CheckStatus.PASS


@pytest.mark.parametrize("item", ["- e.g. this", "- i.e. that", "- the tests pass", "- etc."])
def test_a_list_item_of_words_lists_no_file(item: str) -> None:
    report = f"no git\n{item}\n".encode()
    assert status(bundle(patch=None, report=report), "format:patch") is CheckStatus.FAIL


def test_a_binary_patch_section_holds_only_a_binary_patch() -> None:
    prose = "diff --git a/i b/i\nGIT binary patch\nplain words pretending to be a patch\n"
    assert change.patch_problem(prose) is not None


def test_the_patch_may_be_absent_only_when_the_report_says_no_git_and_lists_the_files() -> None:
    assert status(bundle(patch=None, report=NO_GIT_REPORT.encode()), "format:patch") is CheckStatus.PASS
    no_list = b"no git"
    assert status(bundle(patch=None, report=no_list), "format:patch") is CheckStatus.FAIL
    listed_only = b"Files changed:\n- src/app.py\n"
    assert status(bundle(patch=None, report=listed_only), "format:patch") is CheckStatus.FAIL


@pytest.mark.parametrize(
    "patch",
    [
        PATCH,
        PATCH + PATCH.replace("app.py", "b.py"),
        "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n\\ No newline at end of file\n",
        "--- a/x\n+++ b/x\n@@ -0,0 +1,2 @@\n+a\n+b\n",
        "diff --git a/old b/new\nsimilarity index 100%\nrename from old\nrename to new\n",
        "diff --git a/i.png b/i.png\nnew file mode 100644\nBinary files /dev/null and b/i.png differ\n",
        "diff --git a/i b/i\nGIT binary patch\nliteral 5\nMcmZQzU|?imU;qRE\n\nliteral 0\nHcmV?d00001\n\n"
        + PATCH,
        "commit abc\nAuthor: me\n\n    a message\n\n" + PATCH,
    ],
)
def test_real_patches_parse(patch: str) -> None:
    assert change.patch_problem(patch) is None


@pytest.mark.parametrize(
    "patch",
    [
        "",
        "all done, nothing to see\n",
        "--- a/x\n+++ b/x\n",  # no hunk, no git header: nothing changed
        "--- a/x\n+++ b/x\n@@ -1,2 +1,2 @@\n a\n",  # a hunk shorter than its header
        "--- a/x\n+++ b/x\n@@ -1,1 +1,1 @@\n-a\n+b\n+c\n",  # a line past it
        "--- a/x\n+++ b/x\n@@ nonsense @@\n",
        "@@ -1 +1 @@\n-a\n+b\n",  # a hunk of no file
        PATCH + "and then some prose\n",
    ],
)
def test_bad_patches_do_not(patch: str) -> None:
    assert change.patch_problem(patch) is not None


def test_a_patch_cut_at_the_cap_may_end_mid_hunk() -> None:
    cut = PATCH.rsplit("\n", 3)[0] + "\n"
    assert change.patch_problem(cut) is not None
    assert change.patch_problem(cut, truncated=True) is None


def test_real_pngs_decode() -> None:
    assert change.png_problem(png()) is None
    assert change.png_problem(png(1, 1)) is None


@pytest.mark.parametrize(
    "bad",
    [
        b"",
        b"GIF89a",
        b"\x89PNG\r\n\x1a\n",
        png()[:-12],  # no IEND
        png()[:40] + b"\x00" + png()[41:],  # a flipped byte: its chunk's CRC no longer holds
        png(ihdr=struct.pack(">IIBBBBB", 0, 2, 8, 2, 0, 0, 0)),
        png(ihdr=struct.pack(">IIBBBBB", 2, 2, 7, 2, 0, 0, 0)),  # no such bit depth
        png(raw=b"\x00\xff"),  # short pixels
        png(raw=b"\x09" + b"\xff" * 6 + b"\x00" + b"\xff" * 6),  # no such row filter
    ],
)
def test_broken_pngs_are_named(bad: bytes) -> None:
    assert change.png_problem(bad) is not None


def test_a_decompression_bomb_is_refused_before_it_inflates() -> None:
    """A tiny file claiming a huge canvas: refused on its header, the way Pillow's guard does."""
    assert "outside the bounds" in (change.png_problem(png(100_000, 100_000, raw=b"\x00")) or "")
    # A canvas in bounds whose stream inflates past it stops at the canvas.
    bomb = png(16, 16, raw=b"\x00" * (64 * 1024 * 1024))
    assert change.png_problem(bomb) == "the pixels run past the canvas"


def test_truncation_is_said_in_the_verdict() -> None:
    long_report = REPORT.encode() + b"x" * (change.MAX_TEXT_BYTES + 10)
    shots = {f"s{i:03}.png": png() for i in range(change.MAX_SCREENSHOTS + 3)}
    shots["huge.png"] = b"\x89PNG" + b"\x00" * (change.MAX_SCREENSHOT_BYTES + 1)
    checks = by_name(bundle(report=long_report, tests=b"t" * (change.MAX_TEXT_BYTES + 1), shots=shots))
    said = checks["format:truncated"]
    assert said.status is CheckStatus.SKIP
    assert "report.md" in said.detail and "test-output.txt" in said.detail
    assert "screenshot" in said.detail
    assert "huge.png" not in said.detail  # a name is the agent's: it stays out of the detail


def test_a_huge_patch_is_cut_and_still_parses() -> None:
    hunk = "".join(f"+line {i}\n" for i in range(90_000))
    patch = f"--- a/x\n+++ b/x\n@@ -0,0 +1,90000 @@\n{hunk}"
    assert len(patch) > change.MAX_TEXT_BYTES
    checks = by_name(bundle(patch=patch.encode()))
    assert checks["format:patch"].status is CheckStatus.PASS
    assert "patch.diff" in checks["format:truncated"].detail


def test_a_tar_of_a_million_names_stops_at_the_member_cap() -> None:
    entries: dict[str, bytes | None] = {f"junk/{i}": b"" for i in range(change.MAX_MEMBERS + 5)}
    assert "members" in by_name(bundle(entries))["format:truncated"].detail


class _Frames:
    """`new_frame` for the test: any frame the suite asked for lands here, and records its execs."""

    def __init__(self) -> None:
        self.made: list[FakeFrame] = []

    def __call__(self, task_id: str, **_: Any) -> FakeFrame:
        self.made.append(FakeFrame(task_id))
        return self.made[-1]


async def test_change_suite_never_execs_in_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify judges what was captured during the step and re-runs nothing. No frame is
    made, so there is nowhere to exec, in the user's folder or beside it."""
    frames = _Frames()
    monkeypatch.setattr(verification, "new_frame", frames)
    monkeypatch.setattr(verification, "ensure_ready", lambda *a, **k: None)
    ran = await verification._run_suite(  # pyright: ignore[reportPrivateUsage]
        suite=VerifierSuite.CHANGE,
        artifact_name="/artifacts/change",
        payload=bundle(),
        sha256="0" * 64,
        sources={},
        allow_domains=[],
    )
    assert [c for c in ran.checks if c.status is CheckStatus.FAIL] == [] and ran.preview is None
    assert frames.made == []  # the fake would have recorded `.execs`: there is no frame at all


async def test_other_suites_still_get_their_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    frames = _Frames()
    monkeypatch.setattr(verification, "new_frame", frames)
    monkeypatch.setattr(verification, "ensure_ready", lambda *a, **k: None)

    async def suite(*a: Any, **k: Any) -> list[Any]:
        return []

    monkeypatch.setattr(pipeline, "run_suite", suite)
    await verification._run_suite(  # pyright: ignore[reportPrivateUsage]
        suite=VerifierSuite.DOCUMENT,
        artifact_name="/artifacts/a.md",
        payload=b"x",
        sha256="0" * 64,
        sources={},
        allow_domains=[],
    )
    assert len(frames.made) == 1
