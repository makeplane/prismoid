# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The artifact-kind registry is the single source of truth (ADR-015) - drift is a test failure.

Five independent format lists once coexisted (registry, streaming gate, publish docstring, and two
web-side tables) and had already drifted: the publish docstring advertised a dataset without
`.jsonl`, and the web previewed types that do not exist. These tests pin the derivations so a new
kind added to `ARTIFACT_TYPES` is *automatically* streamable, publishable-by-docstring, verifiable,
and honestly rendered - or the suite says which wire is loose.
"""

from __future__ import annotations

from collections.abc import Sequence

from prismoid.ai.frames.layout import STANDARD_PATHS
from prismoid.ai.tools.frame_tools import TEXT_SUFFIXES, frame_toolset
from prismoid.ai.verification.pipeline import SCRIPTS, SUITES
from prismoid.schemas.outputs import (
    ARTIFACT_TYPES,
    DEVICE_ONLY,
    ArtifactType,
    RenderHint,
    is_text_path,
    publish_types_doc,
    render_for,
    text_artifact_suffixes,
)

LEGACY = {ArtifactType.COUNCIL_OPINION, ArtifactType.COUNCIL_REPORT}


class _StubFrame:
    paths = STANDARD_PATHS

    async def write_file(self, path: str, content: str) -> None: ...
    async def write_bytes(self, path: str, data: bytes) -> None: ...
    async def read_file(self, path: str) -> str:
        return ""

    async def read_bytes(self, path: str) -> bytes:
        return b""

    async def exec(self, command: str, timeout: float = 60.0):  # noqa: ASYNC109
        raise NotImplementedError

    async def publish(self, path: str, type: str, title: str, sources: Sequence[str] = ()) -> None: ...


# ── every kind has a suite (or the pipeline says so honestly) ─────────────────────────────────


def test_every_type_routes_to_a_suite_script_that_exists() -> None:
    """A kind whose suite is missing does not crash a step - it ships UNVERIFIED via the
    `suite:built` SKIP - but the *registry* should never knowingly point at nothing.

    The ONE deliberate exception: IMAGE. Working-role figures never enqueue
    verification, so `image-suite` stays registered-unbuilt until a deliverable image lane
    exists - and this test names that, so a second unbuilt suite cannot hide behind it.

    CHANGE is built and has no script: it never opens a frame (`verifier/change.py`),
    so the worker reads it, and a script here would be a way to run something for it."""
    for artifact_type, spec in ARTIFACT_TYPES.items():
        if artifact_type is ArtifactType.IMAGE:
            assert SCRIPTS.get(spec.verifier_suite) is None, (
                "image-suite grew a script - delete this exception and let the law hold"
            )
            continue
        if artifact_type is ArtifactType.CHANGE:
            assert SCRIPTS.get(spec.verifier_suite) is None, "a change is read, never run in a frame"
            continue
        script = SCRIPTS.get(spec.verifier_suite)
        assert script is not None, f"{artifact_type.value} has no suite script registered"
        assert (SUITES / script).is_file(), f"{script} is registered but not on disk"


# ── the streaming gate derives from the registry ──────────────────────────────────────────────


def test_the_streaming_gate_contains_every_registry_text_extension() -> None:
    for ext in text_artifact_suffixes():
        assert ext in TEXT_SUFFIXES, f"{ext} is registry text but would not stream"


# ── the publish docstring derives from the registry ───────────────────────────────────────────


def test_the_publish_tool_advertises_every_publishable_type_and_extension() -> None:
    doc = frame_toolset(("frame.publish",), _StubFrame()).tools["publish"].description or ""
    for artifact_type, spec in ARTIFACT_TYPES.items():
        if artifact_type in LEGACY:
            assert artifact_type.value not in doc, "legacy wire values are not publish vocabulary"
            continue
        if artifact_type is ArtifactType.RECORDING:
            # the engine's own evidence: the door refuses it from any agent, so no agent is told of it
            assert f"**{artifact_type.value}**" not in doc
            continue
        if artifact_type in DEVICE_ONLY:
            # offered only to a frame on the device (`test_device_law`): to the cloud it is no word
            assert f"**{artifact_type.value}**" not in doc
            continue
        assert artifact_type.value in doc, f"the model is never told {artifact_type.value} exists"
        for ext in spec.extensions:
            assert ext in doc, f"the model is never told {artifact_type.value} accepts {ext}"


def test_publish_types_doc_covers_the_registry() -> None:
    doc = publish_types_doc()
    assert ".jsonl" in doc  # the exact extension the hand-typed docstring had lost


# ── render honesty: the file decides, not the type ────────────────────────────────────────────


def test_render_for_is_path_aware() -> None:
    table = {
        (ArtifactType.DOCUMENT, "memo.md"): RenderHint.MARKDOWN,
        (ArtifactType.DOCUMENT, "memo.txt"): RenderHint.TEXT,
        (ArtifactType.DOCUMENT, "memo.docx"): RenderHint.NONE,  # the mojibake fix, at the source
        (ArtifactType.DOCUMENT, "memo.pdf"): RenderHint.PDF,  # the browser IS the viewer
        # .html: NONE on purpose - raw markup must never ride the text renderer;
        # the sandboxed page-cage preview is the presentation.
        (ArtifactType.DOCUMENT, "page.html"): RenderHint.NONE,
        # Records get a TABLE, not a wall of raw JSON - the scatter map in the reader
        # was the found-live counter-example.
        (ArtifactType.DATASET, "rows.csv"): RenderHint.DATASET,
        (ArtifactType.DATASET, "rows.jsonl"): RenderHint.DATASET,
        (ArtifactType.DATASET, "rows.parquet"): RenderHint.NONE,
        (ArtifactType.SPREADSHEET, "model.xlsx"): RenderHint.NONE,
        (ArtifactType.DECK, "pitch.pptx"): RenderHint.NONE,
        (ArtifactType.CODE, "project"): RenderHint.NONE,  # a tar, whatever the type's streaminess
        (ArtifactType.FLEET_REPORT, "table.md"): RenderHint.MARKDOWN,
    }
    for (artifact_type, filename), expected in table.items():
        assert render_for(artifact_type, filename) is expected, f"{artifact_type.value}/{filename}"


def test_is_text_path_speaks_for_files_not_types() -> None:
    assert is_text_path("/artifacts/memo.md")
    assert is_text_path("/artifacts/rows.csv")
    assert not is_text_path("/artifacts/memo.docx")
    assert not is_text_path("/artifacts/project")  # a tarred directory has no text suffix
    assert not is_text_path("/artifacts/page.html")  # accepted, but never streamed as text


def test_html_is_an_accepted_document_extension() -> None:
    # A static page IS a document; the App kind stays for the interactive lane.
    from prismoid.schemas.outputs import ARTIFACT_TYPES, accepts

    assert ".html" in ARTIFACT_TYPES[ArtifactType.DOCUMENT].extensions
    assert accepts(ArtifactType.DOCUMENT, "/artifacts/launch.html")
