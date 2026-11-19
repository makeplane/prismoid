# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The artifact-type registry and the agent output schemas (the `output_type=` of each agent).

`frame.publish()` validates against `ARTIFACT_TYPES` - an extension the registry does not know
is not publishable, which is how "artifacts exist only via publish" stays true at the
door rather than in a code review.

The `verifier_suite` on each type is the promise the pipeline keeps: an `.xlsx` will have every
formula cell recomputed and cross-footed before anyone sees it.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from prismoid.schemas.collection import Collection


class ArtifactType(StrEnum):
    DOCUMENT = "document"
    SPREADSHEET = "spreadsheet"
    DECK = "deck"
    CODE = "code"
    WEBAPP = "webapp"
    DATASET = "dataset"
    COLLECTION = "collection"  # fields+rows+views JSON; views are lenses
    COUNCIL_OPINION = "council_opinion"  # legacy wire value - historical rows only
    COUNCIL_REPORT = "council_report"  # legacy wire value - pre-rename fleet tables
    FLEET_REPORT = "fleet_report"  # fleet mode: the chair's comparison table
    IMAGE = "image"  # a generated figure: published artifacts are the ONLY door across
    # steps, so a section's PNGs cross into assembly's frame as working-role rows. A deliverable
    # image (shelf-facing) is deferred; today every image row rides role="working".
    RECORDING = "recording"  # a browser step's screenshot-per-action trail:
    # a directory of index.html + actions.json + per-action images, tarred like code/webapp.
    CHANGE = "change"  # a coding task on the device: the work stays in
    # the user's folder, and this directory of patch.diff + report.md + test-output.txt +
    # screenshots/*.png is what crosses. The repo itself is never tarred.


class VerifierSuite(StrEnum):
    """Named suites. Each is a pipeline in `ai/verification/` - deterministic, then judged."""

    DOCUMENT = "document-suite"
    SPREADSHEET = "spreadsheet-suite"
    DECK = "deck-suite"
    CODE = "code-suite"
    DATASET = "dataset-suite"
    COLLECTION = "collection-suite"
    FLEET = "fleet-suite"  # the quote-match suite
    COUNCIL = "council-suite"  # legacy wire value - pre-rename verification rows only
    IMAGE = "image-suite"  # DELIBERATELY unbuilt: working images never enqueue
    # verification (role filter), and a manual `artifacts verify` on one lands the honest
    # `suite:built` SKIP → UNVERIFIED rather than a pretend green. Built when a deliverable
    # image lane exists.
    RECORDING = "recording-suite"  # structural only, no model: tar opens, images
    # exist, the action count matches the journal - never a claim about what the actions meant.
    CHANGE = "change-suite"  # the published bytes and a judge, no frame (`verifier/change.py`): it
    # never re-runs anything, in the user's folder or beside it.


class SmokePolicy(StrEnum):
    """How far the verifier goes to prove the thing actually *runs*."""

    NONE = "none"
    REBUILD = "rebuild"  # rebuild in a fresh frame and diff the output
    BROWSER = "browser"  # rebuild, serve, and drive it with Playwright (webapps)


class RenderHint(StrEnum):
    """How a client should present an artifact's bytes - **derived server-side, never guessed
    client-side**. The web's old hand-kept preview list drifted into rendering `.docx`
    zip bytes as markdown; this enum is the registry answering the question once, on the wire."""

    MARKDOWN = "markdown"
    TEXT = "text"
    COLLECTION = "collection"  # fields+rows+views JSON - the client renders its lenses
    DATASET = "dataset"  # jsonl/csv records - the client renders a table preview
    PDF = "pdf"  # the browser's own PDF viewer renders the presigned bytes
    CHANGE = "change"  # a change's tar: the client lays out its report, diff and screenshots
    NONE = "none"  # binary or otherwise unpreviewable: download is the honest presentation


class ArtifactTypeSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: ArtifactType
    extensions: tuple[str, ...]  # empty ⇒ a directory, not a file
    verifier_suite: VerifierSuite
    smoke: SmokePolicy = SmokePolicy.NONE
    streams_preview: bool = False  # text-like types stream deltas; binaries stream facts
    # The subset of `extensions` whose bytes are UTF-8 text. `document` accepts `.docx`, but a
    # `.docx` is a zip - conflating "this type can be text" with "this file is text" is exactly
    # how mojibake reached both the judge prompt and the web preview.
    text_extensions: tuple[str, ...] = ()
    # Whether the cross-family judge can examine it. A recording is screenshots and an
    # action log the judge prompt never opens, beside checks that are all structural, so its
    # opinion would decide the receipt blind: such a type records no judge, and its suite stands.
    judgeable: bool = True


ARTIFACT_TYPES: Final[dict[ArtifactType, ArtifactTypeSpec]] = {
    ArtifactType.DOCUMENT: ArtifactTypeSpec(
        type=ArtifactType.DOCUMENT,
        # `.html`: a static, self-contained page IS a document - the interactive
        # App kind stays separate. Deliberately NOT in text_extensions: raw markup through
        # the text renderer would be the mojibake path's cousin - the sandboxed page-cage
        # preview is the presentation, and the suite's extraction is the judge's eyes.
        extensions=(".md", ".docx", ".pdf", ".txt", ".html"),
        verifier_suite=VerifierSuite.DOCUMENT,
        streams_preview=True,
        text_extensions=(".md", ".txt"),
    ),
    ArtifactType.SPREADSHEET: ArtifactTypeSpec(
        type=ArtifactType.SPREADSHEET,
        extensions=(".xlsx",),
        verifier_suite=VerifierSuite.SPREADSHEET,
    ),
    ArtifactType.DECK: ArtifactTypeSpec(
        type=ArtifactType.DECK,
        extensions=(".pptx",),
        verifier_suite=VerifierSuite.DECK,
    ),
    ArtifactType.CODE: ArtifactTypeSpec(
        type=ArtifactType.CODE,
        extensions=(),  # a directory: tarred, rebuilt, tested
        verifier_suite=VerifierSuite.CODE,
        smoke=SmokePolicy.REBUILD,
        streams_preview=True,
    ),
    ArtifactType.WEBAPP: ArtifactTypeSpec(
        type=ArtifactType.WEBAPP,
        extensions=(),
        verifier_suite=VerifierSuite.CODE,
        smoke=SmokePolicy.BROWSER,  # a preview URL exists only after the smoke test passes
    ),
    ArtifactType.DATASET: ArtifactTypeSpec(
        type=ArtifactType.DATASET,
        extensions=(".csv", ".parquet", ".jsonl"),
        verifier_suite=VerifierSuite.DATASET,
        streams_preview=True,  # heads only
        text_extensions=(".csv", ".jsonl"),
    ),
    ArtifactType.COLLECTION: ArtifactTypeSpec(
        type=ArtifactType.COLLECTION,
        # A compound suffix: `accepts()` endswith-matches, so this works with zero door changes -
        # and it still ends in `.json`, so `write_file` streams the draft on Lane 2 for free.
        extensions=(".collection.json",),
        verifier_suite=VerifierSuite.COLLECTION,
        streams_preview=True,  # the JSON is text; the judge reads it verbatim (clipped, announced)
        text_extensions=(".collection.json",),
    ),
    ArtifactType.COUNCIL_OPINION: ArtifactTypeSpec(
        type=ArtifactType.COUNCIL_OPINION,
        extensions=(".md",),
        verifier_suite=VerifierSuite.COUNCIL,
        streams_preview=True,
        text_extensions=(".md",),
    ),
    ArtifactType.COUNCIL_REPORT: ArtifactTypeSpec(
        type=ArtifactType.COUNCIL_REPORT,
        extensions=(".md",),
        verifier_suite=VerifierSuite.COUNCIL,
        streams_preview=True,
        text_extensions=(".md",),
    ),
    ArtifactType.FLEET_REPORT: ArtifactTypeSpec(
        type=ArtifactType.FLEET_REPORT,
        extensions=(".md",),
        verifier_suite=VerifierSuite.FLEET,
        streams_preview=True,
        text_extensions=(".md",),
    ),
    ArtifactType.IMAGE: ArtifactTypeSpec(
        type=ArtifactType.IMAGE,
        extensions=(".png", ".jpg", ".webp"),
        verifier_suite=VerifierSuite.IMAGE,  # registered-unbuilt - see the enum comment
    ),
    ArtifactType.RECORDING: ArtifactTypeSpec(
        type=ArtifactType.RECORDING,
        extensions=(),  # a directory: tarred, structurally re-opened, never rebuilt
        verifier_suite=VerifierSuite.RECORDING,
        judgeable=False,
    ),
    ArtifactType.CHANGE: ArtifactTypeSpec(
        type=ArtifactType.CHANGE,
        extensions=(),  # a directory: tarred, read as data, never rebuilt
        verifier_suite=VerifierSuite.CHANGE,
    ),
}

# The types only a frame on the device publishes. To a cloud frame each is what it was before it
# existed, an unknown word: the fresh-folder + `test.sh` contract stays there.
DEVICE_ONLY: Final[frozenset[ArtifactType]] = frozenset({ArtifactType.CHANGE})


def text_artifact_suffixes() -> tuple[str, ...]:
    """Every extension the registry declares as UTF-8 text, deduped - the one definition the
    streaming gate (`frame_tools.TEXT_SUFFIXES`) and the judge's deliverable view derive from."""
    return tuple(dict.fromkeys(ext for spec in ARTIFACT_TYPES.values() for ext in spec.text_extensions))


def is_text_path(path: str) -> bool:
    """Is this *file* (not merely its type) UTF-8 text, per the registry?"""
    return path.lower().endswith(text_artifact_suffixes())


def render_for(artifact_type: ArtifactType, filename: str) -> RenderHint:
    """The registry's one answer to "how should a client present this file?".

    Path-aware because types span media: a `document` is markdown at `.md` and a zip at `.docx`.
    """
    if artifact_type is ArtifactType.CHANGE:
        return RenderHint.CHANGE
    spec = ARTIFACT_TYPES[artifact_type]
    name = filename.lower()
    if name.endswith(".pdf"):
        # Not a text hint and not "none": the bytes themselves are the preview - every browser
        # ships a sandboxed PDF renderer, so the client embeds the presigned URL.
        return RenderHint.PDF
    if not any(name.endswith(ext) for ext in spec.text_extensions):
        return RenderHint.NONE
    if artifact_type is ArtifactType.COLLECTION:
        return RenderHint.COLLECTION
    if artifact_type is ArtifactType.DATASET:
        # .csv/.jsonl are RECORDS, not prose: the raw-text pre was the scatter lane's wall of
        # JSON in the reader - the client renders a table preview.
        return RenderHint.DATASET
    return RenderHint.MARKDOWN if name.endswith(".md") else RenderHint.TEXT


def publish_types_doc(*, on_host: bool = False) -> str:
    """The publish tool's type vocabulary, generated from the registry - the docstring the model
    reads can no longer drift from the door it describes (it once advertised a dataset list that
    was missing `.jsonl`). `on_host`: the frame is on the device, where `DEVICE_ONLY` is offered."""
    # Historical wire values, and the recording: the engine's own evidence, which the
    # publish door refuses from every agent - offering it cost every publisher's prompt a type
    # it could only ever be refused.
    unoffered = {ArtifactType.COUNCIL_OPINION, ArtifactType.COUNCIL_REPORT, ArtifactType.RECORDING}
    if not on_host:
        unoffered |= DEVICE_ONLY
    parts: list[str] = []
    for artifact_type, spec in ARTIFACT_TYPES.items():
        if artifact_type in unoffered:
            continue
        exts = " ".join(spec.extensions) if spec.extensions else "a directory"
        parts.append(f"**{artifact_type.value}** ({exts})")
    return " · ".join(parts)


def spec_for(artifact_type: ArtifactType) -> ArtifactTypeSpec:
    return ARTIFACT_TYPES[artifact_type]


def accepts(artifact_type: ArtifactType, path: str) -> bool:
    """Does this type accept this path? A directory type accepts anything without a suffix."""
    spec = ARTIFACT_TYPES[artifact_type]
    if not spec.extensions:
        return "." not in path.rsplit("/", 1)[-1]
    return any(path.endswith(ext) for ext in spec.extensions)


class OutputRef(BaseModel):
    """What a step is expected to produce. The plan says it before the agent tries."""

    model_config = ConfigDict(frozen=True)

    type: ArtifactType
    title: str
    unverified_ok: bool = False  # nothing unverified ships without an explicit mark
    filename: str | None = None  # the name the user asked for; the system's publish keeps it


def slug_title(title: str) -> str:
    """A title as a file stem: lowercase words joined by dashes, at most 60 characters."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60].strip("-")
    return slug or "document"


def document_filename(output: OutputRef | None, kind: ArtifactType, stem: str) -> str:
    """The name the user asked for, or the title's, with only the kind's text extensions in the
    registry's own spelling (these bytes are the model's text: a `report.pdf` would be a lie)."""
    named = (output.filename if output else None) or ""
    for suffix in spec_for(kind).text_extensions:
        if named.lower().endswith(suffix):
            return named[: -len(suffix)] + suffix
    return f"{stem}.md"


# ── agent output schemas (`output_type=outputs.get(...)`) ─────────────────────────────────────
# An agent's `output_ref` names one of these. Typed outputs are why the kernel never parses prose.


class Narration(BaseModel):
    """What an agent *did*, for an agent whose work is not itself prose.

    NOT the default. `published` is derived from the manifest - the system knows what was
    published because publish is the only door; asking the model to *tell* us what it published is
    asking it to duplicate a fact we already hold, and to be wrong about it.
    """

    summary: str = Field(min_length=1)
    published: list[str] = Field(default_factory=list)


class Finding(BaseModel):
    claim: str
    source: str  # a URL or a frame path - a claim without a source is not a finding
    confidence: float = Field(ge=0.0, le=1.0)


class Research(BaseModel):
    findings: list[Finding]
    summary: str


class CollectionResult(BaseModel):
    """A collection step's one-call deliverable : the collection itself, TYPED.

    The provider is schema-constrained, pydantic-ai retries a validation miss INSIDE the call
    with the error in hand, and the SYSTEM writes + publishes the file through the one door.
    The forgot-to-publish and malformed-JSON retry classes cannot exist on this path - which is
    both the accuracy and the speed: one streaming call instead of writing a file, publishing it
    and narrating the close."""

    filename: str = Field(
        default="collection",
        pattern=r"^[a-z0-9][a-z0-9-]{0,60}$",
        description="kebab-case basename; the system writes <filename>.collection.json",
    )
    summary: str = Field(
        min_length=1,
        description=(
            "THE THREAD'S ANSWER, markdown. A findings/list ask leads with the data itself: "
            "a compact GFM table of the asked-for columns (cap ~15 rows, then one line: "
            "'all N rows are in the collection'), one quiet build sentence after. An "
            "organize/plan ask, where the breakdown is the artifact, keeps 1-3 sentences on "
            "what was built. Never view-machinery talk in place of the answer."
        ),
    )
    collection: Collection


# ── the typed ending ───────────────────────────────────────────────────────────────────────────
# A guarded agent (the computer) ends by calling `done` with an Outcome: what it did, in the
# user's words, and how the system can see it took effect - never free text the executor has to
# believe. `DONE_TOOL` is the output tool's own name, so the prompt can say "finish with `done`".

OutcomeStatus = Literal["done", "partial", "blocked", "failed"]
BlockedBy = Literal["login", "approval", "person", "budget"]
# Whose session a change on a site landed in: `guest` - nobody signed in (an anonymous cart lives
# in this browser, not the person's account); `signed_in` - the person's own account. None: no
# account or cart was involved.
SessionKind = Literal["guest", "signed_in"]
DONE_TOOL: Final = "done"


class SiteBlock(StrEnum):
    """What stopped the browser reaching a site, as the browser reported it (wire values: the
    sidecar says the first three about an open, the browser tool the rest), never read off words."""

    EGRESS = "egress"  # the frame's egress proxy refused the open
    RESET = "reset"  # the site reset the stream on this browser, once
    UNRESOLVED = "unresolved"  # the site's name did not resolve
    WALL = "wall"  # the same site reset it again: a bot wall in front of it
    REFUSED = "refused"  # the browser policy refused the address before any open
    CHALLENGED = "challenged"  # the page came back empty: the sandbox refused hosts it needed


class Proof(BaseModel):
    """How the system can see the work took effect. It is checked before a `done` counts."""

    url: str | None = Field(default=None, description="the page the effect is visible on")
    page_text: str | None = Field(
        default=None, description="a short phrase that must be visible on that page now"
    )
    path: str | None = Field(default=None, description="a /work path that must exist")
    artifact: str | None = Field(default=None, description="the path of a file you published")


class Criterion(BaseModel):
    """One line the step is judged on, accounted for when it finishes."""

    line: int = Field(ge=1, description="its number in the list you will be judged on, from 1")
    met: bool
    shows: str = Field(
        min_length=1, max_length=300, description="what shows it is met, or what stands in its way"
    )


class Outcome(BaseModel):
    """Finish the step. `done`: it took effect - give a `proof` the system can check. `partial`:
    some of it did. `blocked`: something outside your tools stops you, and `blocked_by` says what.
    `failed`: it could not be done this way."""

    status: OutcomeStatus
    summary: str = Field(
        min_length=1,
        max_length=2000,
        description="what was done, in the user's words - this is the answer they read",
    )
    blocked_by: BlockedBy | None = None
    session: SessionKind | None = Field(
        default=None,
        description="when the change landed in a cart or an account on a site: 'guest' if nobody "
        "signed in during this task, 'signed_in' if the person's account was signed in; leave it "
        "out when no cart or account was involved",
    )
    refused: bool = Field(
        default=False,
        description="true when you decline on purpose - the request is unsafe or the person's own "
        "words rule it out; a refusal is final, never pushed past",
    )
    proof: Proof | None = None
    criteria: list[Criterion] = Field(
        default_factory=list[Criterion],
        max_length=20,
        description="when you are told what you will be judged on: one entry per line, met or not; "
        "a `done` needs every line met",
    )


# The writer's output tool: the document goes in its arguments, so nothing said to the requester reaches it.
SUBMIT_DOCUMENT: Final = "submit_document"


class SubmittedDocument(BaseModel):
    """The writer's whole deliverable, typed: the document the reader receives and nothing addressed to
    the requester. The system writes the title as its heading above `content` and publishes the file."""

    title: str = Field(min_length=1, max_length=200, description="the document's title; it names the file")
    content: str = Field(
        min_length=1,
        description="the whole document in markdown, below its title: no preamble, no closing remarks, "
        "no account of what you did",
    )

    @property
    def markdown(self) -> str:
        """The published text: the title as the one `#` heading, then the content."""
        heading = f"# {self.title.strip()}"
        body = self.content.strip()
        return body if body.startswith(f"{heading}\n") or body == heading else f"{heading}\n\n{body}\n"


# A long document's part tool (the writer's `section` mode): one part, and the summary the later parts read.
SUBMIT_PART: Final = "submit_part"


class SubmittedPart(BaseModel):
    """One part of a long document, typed: the part's markdown, which the system publishes under the part's own
    name with its `[S#]` marks kept for the one render at assembly, and its summary for the parts after it."""

    content: str = Field(
        min_length=1,
        description="the whole part in markdown, from its `##` heading: no preamble, no account of what you did",
    )
    summary: str = Field(
        min_length=1,
        max_length=1200,
        description="two to four sentences: what this part established, for the parts written after it",
    )


_OUTPUTS: Final[dict[str, type[BaseModel]]] = {
    "narration": Narration,
    "research": Research,
    "collection_result": CollectionResult,
    "outcome": Outcome,
    "document": SubmittedDocument,
    "part": SubmittedPart,
}
# An output ref whose tool has a name of its own. Every other typed output keeps
# pydantic-ai's `final_result` - the draft tap pours a collection by that name.
_OUTPUT_TOOLS: Final[dict[str, str]] = {
    "outcome": DONE_TOOL,
    "document": SUBMIT_DOCUMENT,
    "part": SUBMIT_PART,
}


def output_tool_name(output_ref: str | None) -> str | None:
    """The output tool's own name for a ref that has one (`done` for the Outcome), else None."""
    return _OUTPUT_TOOLS.get(output_ref) if output_ref is not None else None


def get(output_ref: str | None) -> type[BaseModel] | type[str]:
    """Resolve an AgentDef's `output_ref`. Unknown refs are a registry bug, not a runtime choice.

    **`None` means text, and that is the right default.** A typed output is a *tool call* under the
    hood - the model must emit a JSON object matching a schema. That is exactly what you want from
    a researcher returning findings, and exactly the wrong hoop to make a **writer** jump through:
    its deliverable *is* the prose. Wrapping a memo in `{"summary": "..."}` buys nothing, and it
    costs a schema-compliance failure on any model that is not a frontier one.

    Structure where structure is the product; text where text is.
    """
    if output_ref is None:
        return str
    return _OUTPUTS[output_ref]


class TaskTitle(BaseModel):
    """The namer's whole output: one shelf label. Length is enforced by the operation (a
    model that overruns is truncated, not retried - a title is not worth a second call)."""

    title: str


MemoryKindWord = Literal["instruction", "fact", "preference"]


class MemoryProposal(BaseModel):
    """One durable sentence a `bot_lesson` proposes for a bot's ledger. No scope field: code
    picks the lane, so there is no classification to get wrong."""

    kind: MemoryKindWord
    text: str


class QuotedMemory(BaseModel):
    """One memory `memory_extract` proposes: the person's own words, quoted. Code keeps it only
    when `quote` is verbatim in what they said and `lasts` is `standing`."""

    kind: MemoryKindWord
    quote: str = Field(description="The person's words, copied exactly, that say the whole memory.")
    lasts: Literal["standing", "this_task"] = Field(
        description="`standing` when their words say it holds beyond this task; `this_task` when it "
        "shapes only the work in front of them."
    )
    replaces: int | None = Field(
        default=None, description="The number of an already-remembered line this corrects or updates."
    )


class MemoryExtract(BaseModel):
    """`memory_extract`'s whole output. **An empty list is the expected answer**: most turns
    teach nothing durable, and code has the final word on what lands."""

    items: list[QuotedMemory] = Field(default_factory=list[QuotedMemory])


def register_output(name: str, schema: type[BaseModel]) -> None:
    _OUTPUTS[name] = schema
