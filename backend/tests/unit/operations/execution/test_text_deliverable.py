# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The text deliverable lane - the writer's submitted document, or a text answer, IS the document.

The failure this replaces: a theory-of-consciousness task rode its whole document
inside ONE `write_file` tool call's JSON args, hit the provider's 4,096-token default output cap,
and burned thirteen identical truncations across three ladder rungs - nine minutes, $0.31, nothing
delivered. Two laws fix the class:

  1. the SYSTEM publishes a text-deliverable agent's output through the one door
     (`_publish_document_text` - no publish ceremony in the model's hands), and
  2. the output ceiling is SET (`default_model_settings`), never left to a provider default.

These pin both, plus the registry markers the engine keys on.
"""

from __future__ import annotations

import uuid

import pytest

from prismoid.ai.agents.base import definition_for
from prismoid.ai.deps import TaskDeps
from prismoid.ai.frames.publish import validate_publish
from prismoid.ai.gateway.providers import default_model_settings
from prismoid.db.models.plan import PlanStep
from prismoid.operations.execution.equip import Equipped
from prismoid.operations.execution.inbox import StepInbox
from prismoid.operations.execution.publish import Output, publish_output, receipt_line
from prismoid.operations.execution.publish import publish_document_text as _publish_document_text
from prismoid.operations.execution.publish import publishes_text as _publishes_text
from prismoid.schemas.contract import Contract, SkillPin
from prismoid.schemas.ids import AgentName, SkillName
from prismoid.schemas.outputs import (
    ARTIFACT_TYPES,
    ArtifactType,
    OutputRef,
    SubmittedDocument,
    accepts,
    document_filename,
)
from prismoid.schemas.outputs import slug_title as _slug_title
from prismoid.schemas.plan import StepKind, StepSpec


class _Frame:
    def __init__(self) -> None:
        self.written: dict[str, bytes] = {}
        self.published: list[tuple[str, str, str, tuple[str, ...]]] = []

    async def write_bytes(self, path: str, body: bytes) -> None:
        self.written[path] = body

    async def read_bytes(self, path: str) -> bytes:
        return self.written[path]

    async def publish(self, path: str, type_: str, title: str, sources: list[str] | None = None) -> None:
        self.published.append((path, type_, title, tuple(sources or ())))


class _Deps:
    def __init__(self) -> None:
        self.inputs = ["/task/inputs/brief.md"]


def _spec(*, output_title: str = "An original proposal") -> StepSpec:
    return StepSpec(
        kind=StepKind.STEP,
        agent=AgentName("writer"),
        title="draft the proposal",
        contract=Contract(
            goal="Write the proposal.",
            acceptance=["complete"],
            inputs=["/task/inputs/brief.md"],
            output=OutputRef(type=ArtifactType.DOCUMENT, title=output_title),
        ),
    )


# ── the registry markers the engine keys on ────────────────────────────────────────────────────


def test_a_collection_maker_stays_the_typed_publisher() -> None:
    organize = SkillPin(name=SkillName("organize"), version=1)
    seated = definition_for("maker", TaskDeps(skill_pins=[organize]))
    assert seated.publishes_output
    assert seated.output_ref == "collection_result"


def test_publishes_text_reads_the_registry_not_a_string() -> None:
    assert _publishes_text(_spec().model_copy(update={"agent": AgentName("researcher")}))
    assert not _publishes_text(_spec())  # the writer submits a typed document - its own branch owns it
    maker = _spec().model_copy(update={"agent": AgentName("maker")})
    assert not _publishes_text(maker)  # it publishes through its tools, or a typed answer
    nobody = _spec().model_copy(update={"agent": AgentName("no-such-agent")})
    assert not _publishes_text(nobody)


# ── the system-side publish ────────────────────────────────────────────────────────────────────


async def test_the_documents_own_heading_names_the_artifact() -> None:
    frame = _Frame()
    facts: list[str] = []

    async def fact(phase: str, label: str, *, started: float | None = None) -> None:
        assert frame.published, "the engine fact lands AFTER the door - never before the act"
        facts.append(label)

    text = "# A Theory of Orchestrated Emergence\n\nThe claim, led with.\n"
    title, filename = await _publish_document_text(frame, _Deps(), _spec(), text, fact=fact)

    assert title == "A Theory of Orchestrated Emergence"
    assert filename == "a-theory-of-orchestrated-emergence.md"
    path = f"/work/{filename}"
    assert frame.written[path] == text.encode()
    assert frame.published == [(path, "document", title, ("/task/inputs/brief.md",))], (
        "the one door, with the frame's inputs stamped as sources"
    )
    assert facts == [f"Published {filename} · document"], "past tense - the publish happened"


async def test_a_headingless_document_falls_back_to_the_plans_declared_title() -> None:
    frame = _Frame()

    async def fact(phase: str, label: str, *, started: float | None = None) -> None: ...

    title, filename = await _publish_document_text(
        frame, _Deps(), _spec(output_title="Quarterly memo"), "Just body prose, no heading.", fact=fact
    )
    assert title == "Quarterly memo"
    assert filename == "quarterly-memo.md"


async def test_a_kept_section_publishes_the_last_documents_text_byte_for_byte() -> None:
    """Live (task 0211d614, turn 3): told to reply with a kept file exactly, Haiku writers
    reproduced clean text byte for byte but reworked text that carried narration - trimmed it, or
    narrated around it. A kept section is the system's copy, never a model's."""
    frame = _Frame()
    kept = "Now I have all the materials.\n\n## Output\n\n**Bottom line: one way out.**\n"
    frame.written["/task/inputs/03-proxies-final.md"] = kept.encode()

    async def fact(phase: str, label: str, *, started: float | None = None) -> None: ...

    spec = _spec(output_title="03 Proxies").model_copy(
        update={
            "agent": AgentName("writer"),
            "lane_role": "section",
            "title": "03 Proxies",
            "kept_from": "/task/inputs/03-proxies-final.md",
        }
    )
    _, filename = await _publish_document_text(frame, _Deps(), spec, "KEPT", fact=fact)
    assert frame.written[f"/work/{filename}"] == kept.encode()


def test_slugs_never_come_out_empty_or_hostile() -> None:
    assert _slug_title("Théorie !!! de la conscience") == "th-orie-de-la-conscience"
    assert _slug_title("!!!") == "document"
    assert "/" not in _slug_title("a/b ../../etc/passwd")


# ── the output ceiling ─────────────────────────────────────────────────────────────────────────


def test_the_output_ceiling_is_set_never_left_to_a_provider_default() -> None:
    """Providers default max_tokens LOW (Anthropic: 4,096) - exactly the cap that truncated a
    full document thirteen times in the live failure. The ceiling is ours to own: high enough
    for any sane deliverable, under every catalog model's output limit."""
    settings = default_model_settings()
    assert settings.get("max_tokens") == 16_000


# ── what the reader gets: the document, under the name they asked for ──────────────────────────


async def _noop(phase: str, label: str, *, started: float | None = None) -> None:
    return None


SUBMITTED = SubmittedDocument(title="Why the Sky Is Blue", content="- Rayleigh scattering.")
NARRATED = SUBMITTED.markdown


class _Result:
    def __init__(self, output: SubmittedDocument) -> None:
        self.output = output

    def all_messages(self) -> list[object]:
        return []


async def test_the_writers_submitted_document_is_published_as_its_title_and_content() -> None:
    """Regression: the writer's whole answer shipped, so the reader's document opened with "Now I'll
    write the report..." and quoted the judgment criteria back at them. Now the document is the
    `submit_document` call's arguments: what the model says beside it never reaches the file."""
    frame = _Frame()
    deps = TaskDeps()
    inbox = StepInbox(uuid.uuid4(), uuid.uuid4(), 1, told=0)
    equipped = Equipped(deps=deps, inputs=(), bots=(), inbox=inbox)
    at = Output(frame=frame, deps=deps, spec=_spec(), step=PlanStep(), equipped=equipped, fact=_noop)
    narration, _ = await publish_output(at, _Result(SUBMITTED), override=None)
    assert frame.written == {
        "/work/why-the-sky-is-blue.md": b"# Why the Sky Is Blue\n\n- Rayleigh scattering.\n"
    }
    assert frame.published[0][2] == "Why the Sky Is Blue"
    assert narration.startswith("Published **Why the Sky Is Blue**")


def test_the_publish_line_names_the_file_once_when_the_title_is_the_file_name() -> None:
    assert receipt_line("Why the Sky Is Blue", "why-the-sky-is-blue.md", 463) == (
        "Published **Why the Sky Is Blue** (`why-the-sky-is-blue.md`, 463 words)."
    )
    assert receipt_line("bicycle-history.md", "bicycle-history.md", 463) == (
        "Published **bicycle-history.md** (463 words)."
    )


def test_a_title_the_content_repeats_heads_the_document_once() -> None:
    repeated = SubmittedDocument(title="Why the Sky Is Blue", content="# Why the Sky Is Blue\n\n- Rayleigh.")
    assert repeated.markdown == "# Why the Sky Is Blue\n\n- Rayleigh."


async def test_an_intro_above_the_first_heading_is_never_cut_on_a_guess() -> None:
    body = "This memo covers Q3 for the board.\n\n# Revenue\n\nUp 4%.\n\n# Costs\n\nFlat.\n"
    frame = _Frame()
    await _publish_document_text(frame, _Deps(), _spec(), body, fact=_noop)
    assert next(iter(frame.written.values())).decode() == body


async def test_a_headingless_or_fenced_document_is_published_whole() -> None:
    for body in ("Here is the answer in one line.\n", "```email\nTo: a@b.c\n\n# Not a title\n```\n"):
        frame = _Frame()
        await _publish_document_text(frame, _Deps(), _spec(), body, fact=_noop)
        assert next(iter(frame.written.values())).decode() == body


def _named(filename: str | None) -> StepSpec:
    spec = _spec()
    assert spec.contract is not None and spec.contract.output is not None
    output = spec.contract.output.model_copy(update={"filename": filename})
    return spec.model_copy(update={"contract": spec.contract.model_copy(update={"output": output})})


async def test_a_file_the_user_named_is_the_file_they_get() -> None:
    frame = _Frame()
    _, filename = await _publish_document_text(frame, _Deps(), _named("sky.md"), NARRATED, fact=_noop)
    assert filename == "sky.md" and "/work/sky.md" in frame.written
    assert frame.published[0][0] == "/work/sky.md"
    assert frame.published[0][2] == "Why the Sky Is Blue"  # the title is still the document's own


async def test_a_named_file_the_door_would_refuse_keeps_the_titles_name() -> None:
    frame = _Frame()
    _, filename = await _publish_document_text(frame, _Deps(), _named("sky.xlsx"), NARRATED, fact=_noop)
    assert filename == "why-the-sky-is-blue.md"


async def test_a_name_in_capitals_keeps_its_stem_and_passes_the_door() -> None:
    frame = _Frame()
    _, filename = await _publish_document_text(frame, _Deps(), _named("SKY.MD"), NARRATED, fact=_noop)
    assert filename == "SKY.md"
    validate_publish("document", frame.published[0][0])


@pytest.mark.parametrize(
    ("kind", "suffix"),
    [(kind, suffix) for kind, spec in ARTIFACT_TYPES.items() for suffix in spec.text_extensions],
)
def test_every_text_extension_in_any_case_is_a_name_the_door_takes(kind: ArtifactType, suffix: str) -> None:
    for spelled in (suffix, suffix.upper(), suffix.title()):
        named = OutputRef(type=kind, title="T", filename=f"Notes{spelled}")
        name = document_filename(named, kind, "stem")
        assert name == f"Notes{suffix}"
        assert accepts(kind, name), "the door's own comparison decides, and there is only the one"


class _FolderOfTheirs(_Frame):
    """A frame on the person's computer: its `/work` is their real folder."""

    staging_dir: str | None = "/out"


@pytest.mark.parametrize(("named", "lands"), [("README.md", "README.md"), (None, "why-the-sky-is-blue.md")])
async def test_on_the_persons_computer_nothing_is_written_into_their_folder(
    named: str | None, lands: str
) -> None:
    """The name is the planner's or the title's, and `/work` here is the person's own repository:
    its `README.md` stays theirs. The published name is the same either way."""
    frame = _FolderOfTheirs()
    spec = _named(named) if named else _spec()
    _, filename = await _publish_document_text(frame, _Deps(), spec, NARRATED, fact=_noop)
    assert filename == lands and list(frame.written) == [f"/out/{lands}"]
    assert frame.published[0][0] == f"/out/{lands}"


async def test_a_frame_with_no_staging_folder_yet_writes_where_a_cloud_frame_does() -> None:
    frame = _FolderOfTheirs()
    frame.staging_dir = None
    _, filename = await _publish_document_text(frame, _Deps(), _named("sky.md"), NARRATED, fact=_noop)
    assert list(frame.written) == [f"/work/{filename}"]
