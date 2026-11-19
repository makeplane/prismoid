# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The attempt's publishing stage: a run's typed or text output, written by the system and walked
through the one door (`frame.publish`), so forgot-to-publish, malformed JSON and clipped tool args
are not failure modes. Each publisher writes canonical bytes, publishes them, then says so on the
progress tree - a publish the door refused never gets a settled row.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

import orjson

from prismoid.ai.agents.base import definition_for
from prismoid.ai.context.citations import UnknownSource, render_citations
from prismoid.ai.deps import TaskDeps
from prismoid.ai.tools.browser_policy import unreachable_target, wall_kind
from prismoid.ai.tools.research import citable
from prismoid.db.models.plan import PlanStep
from prismoid.errors import ModelIncapable
from prismoid.operations import live
from prismoid.operations.execution.device import stage_root
from prismoid.operations.execution.equip import Equipped
from prismoid.operations.execution.errors import IncompleteStep
from prismoid.operations.execution.mounts import narration_of
from prismoid.operations.longform import OUTLINE_FILENAME
from prismoid.operations.swarm import MAP_FILENAME, records_filename
from prismoid.schemas.bots import ResolvedBot
from prismoid.schemas.failures import FailureKind
from prismoid.schemas.guard import outcome_narration
from prismoid.schemas.longform import OutlineResult, render_outline_md
from prismoid.schemas.outputs import (
    ArtifactType,
    CollectionResult,
    Outcome,
    SubmittedDocument,
    SubmittedPart,
    document_filename,
)
from prismoid.schemas.outputs import slug_title as _slug_title
from prismoid.schemas.plan import StepSpec
from prismoid.schemas.swarm import RecordSet, ScatterResult, synthesis_digest

# The typed slot an attempt hands up for the completion path to expand on.
type Typed = OutlineResult | ScatterResult | RecordSet | Outcome


async def publish_collection_result(
    frame: object, deps: object, out: CollectionResult, *, fact: live.ActivityFact
) -> None:
    """The system-side publish for a typed collection: canonical bytes into /work,
    then the one door - same manifest, same harvest, same receipt as any published file. The
    sources stamp mirrors the publish tool's: everything the frame was given. The engine fact
    lands AFTER the door - a publish the door refused never gets a settled row - and without
    it the tree's only trace of a system-side publish is the draft pour."""

    path = f"{stage_root(frame)}/{out.filename}.collection.json"
    body = orjson.dumps(out.collection.model_dump(mode="json"), option=orjson.OPT_INDENT_2)
    await frame.write_bytes(path, body)  # type: ignore[attr-defined]
    await frame.publish(  # type: ignore[attr-defined]
        path,
        ArtifactType.COLLECTION.value,
        out.collection.title,
        sources=list(getattr(deps, "inputs", ()) or ()),
    )
    await fact("end", f"Published {out.filename}.collection.json · collection")


def seat_of(spec: StepSpec) -> str:
    """The seat id from the spec's own stamp - the system states its own fact when the
    model forgot to echo `seat` (the Narration.published lesson). A field, never a title
    parse: run 5's hyphenated ids broke every string-surgery reading of seat identity."""
    return spec.scatter_seat or "seat"


async def publish_scatter_map(
    frame: object, deps: object, scatter: ScatterResult, *, fact: live.ActivityFact
) -> None:
    """The scatter map step's typed map -> `swarm.MAP_FILENAME`, through the one door (every seat,
    synthesis and the tally read it)."""

    # .jsonl, one line - the dataset kind's door accepts .csv/.parquet/.jsonl only (found
    # by the lane's first LIVE run: three identical rungs burned on '.json is not a valid
    # dataset' - a system-side filename the ladder could never fix).
    path = f"{stage_root(frame)}/{MAP_FILENAME}"
    body = orjson.dumps(scatter.model_dump(mode="json"))
    await frame.write_bytes(path, body)  # type: ignore[attr-defined]
    await frame.publish(  # type: ignore[attr-defined]
        path,
        ArtifactType.DATASET.value,
        f"Scatter map - {scatter.entity}",
        sources=list(getattr(deps, "inputs", ()) or ()),
    )
    await fact("end", f"Published scatter-map.jsonl · {len(scatter.seats)} seats")


async def publish_record_set(
    frame: object, deps: object, records: RecordSet, *, round_no: int, fact: live.ActivityFact
) -> None:
    """One seat's harvest → `records-<seat>[-rN].jsonl` (working role): the WANDR record
    atoms, durable and by-reference - the tally and the synthesis read these, never
    anyone's context (by reference). Round-suffixed from round 2: the
    synthesis mount is last-write-wins per path, so a gap seat republishing its fence's
    round-1 name would silently erase the records the tally already counted."""

    filename = records_filename(records.seat or "seat", round_no)  # the dataset door's extensions
    path = f"{stage_root(frame)}/{filename}"
    body = orjson.dumps(records.model_dump(mode="json"))
    await frame.write_bytes(path, body)  # type: ignore[attr-defined]
    await frame.publish(  # type: ignore[attr-defined]
        path,
        ArtifactType.DATASET.value,
        f"Records - seat {records.seat or '?'}",
        sources=list(getattr(deps, "inputs", ()) or ()),
    )
    await fact("end", f"Published {filename} · {len(records.records)} record(s)")


async def publish_outline_result(
    frame: object,
    deps: object,
    outline: OutlineResult,
    *,
    fact: live.ActivityFact,
    filename: str | None = None,
    title: str | None = None,
) -> None:
    """The typed outline as `outline.md` (`longform.OUTLINE_FILENAME`), through the one door: assembly mounts
    it and the QA gate reads it. A consensus proposal passes its own `filename` and `title`."""

    name = filename or OUTLINE_FILENAME
    path = f"{stage_root(frame)}/{name}"
    await frame.write_bytes(path, render_outline_md(outline).encode())  # type: ignore[attr-defined]
    await frame.publish(  # type: ignore[attr-defined]
        path,
        ArtifactType.DOCUMENT.value,
        (title or f"Outline - {outline.title}")[:80],
        sources=list(getattr(deps, "inputs", ()) or ()),
    )
    await fact("end", f"Published {name} · outline")


def publishes_text(spec: StepSpec, bots: tuple[ResolvedBot, ...] = ()) -> bool:
    """Is this step's agent one whose TEXT output is the deliverable? Definition truth, not string
    matching - `publishes_output=True` with no typed `output_ref`.

    **Resolved against this step's bots as well as the registry**. It used to ask the
    registry alone, which is right for every built-in and silently wrong for a bot: the lookup
    raised `KeyError`, this returned False, the engine skipped the system publish, and the step
    "produced nothing" three times before escalating - with the bot's finished work in the run's
    output the whole time. Found by a live run; the fix is to ask the same question `build()`
    asks, which is what `definition_for` is.
    """

    try:
        definition = definition_for(str(spec.agent), TaskDeps(bots=bots))
    except KeyError:
        return False
    return definition.publishes_output and definition.output_ref is None


async def publish_document_text(
    frame: object,
    deps: object,
    spec: StepSpec,
    text: str,
    *,
    fact: live.ActivityFact,
    sources: Mapping[str, str] | None = None,
    marked: bool = False,
) -> tuple[str, str]:
    """Publish a text deliverable through the one door, named after its own `# Title`; each cited source id
    becomes its page's link (or stays marked with `marked`), and an unknown id refuses the publish."""
    if spec.contract is None:
        raise IncompleteStep(spec.title, "contract")

    if spec.kept_from:
        # A kept section is the last document's text byte for byte: the system copies the
        # mounted file - a model told to reproduce it reworks text that carries narration.
        text = (await frame.read_bytes(spec.kept_from)).decode()  # type: ignore[attr-defined]
    first_heading = next(
        (line.lstrip("#").strip() for line in text.splitlines() if line.lstrip().startswith("#")),
        None,
    )
    declared = spec.contract.output.title if spec.contract.output else None
    title = (first_heading or declared or "Document").strip()
    if spec.lane_role == "section" and declared:
        # A part's DECLARED title carries its number ("03 Market analysis"): assembly mounts `nn-*.md` in
        # lexical, which is document, order.
        title = declared.strip()
    kind = spec.contract.output.type if spec.contract.output else ArtifactType.DOCUMENT
    stem = _slug_title(title)
    output = None if spec.lane_role else spec.contract.output  # a lane's names are load-bearing
    path = f"{stage_root(frame)}/{document_filename(output, kind, stem)}"
    filename = path.rsplit("/", 1)[-1]
    if sources is not None:
        text = _cited(text, sources, marked=marked)
    await frame.write_bytes(path, text.encode())  # type: ignore[attr-defined]
    await frame.publish(  # type: ignore[attr-defined]
        path, kind.value, title, sources=list(getattr(deps, "inputs", ()) or ())
    )
    await fact("end", f"Published {filename} · {kind.value}")
    return title, filename


def _cited(text: str, sources: Mapping[str, str], *, marked: bool) -> str:
    """The text with its citations rendered as links, or kept as `[S#]` marks when `marked`; an unknown
    id is the attempt's death, continued with the reason so the model fixes its citations."""
    try:
        rendered = render_citations(text, sources)
    except UnknownSource as unknown:
        raise ModelIncapable(
            f"the answer cites a source this task never read: {unknown}", pickup="continue"
        ) from None
    return text if marked else rendered


@dataclass(frozen=True)
class Output:
    """The run's ending and where it publishes."""

    frame: object
    deps: TaskDeps
    spec: StepSpec
    step: PlanStep
    equipped: Equipped
    fact: live.ActivityFact

    @property
    def bots(self) -> tuple[ResolvedBot, ...]:
        """The bots the step may be served by."""
        return self.equipped.bots


async def publish_output(at: Output, result: Any, *, override: Outcome | None) -> tuple[str, Typed | None]:
    """A publishing agent's output publishes itself: canonical bytes through the one door. Returns the
    narration and the typed slot the completion path expands on (`outline`)."""
    output = result.output
    narration: str | None = None
    outline: Typed | None = None
    if isinstance(output, Outcome):
        outline, narration = _ended(result, override)
    elif isinstance(output, CollectionResult):
        await publish_collection_result(at.frame, at.deps, output, fact=at.fact)
    elif isinstance(output, SubmittedDocument):
        narration = await _document(at, output.markdown, _sources(at, result))
    elif isinstance(output, SubmittedPart):
        narration = await _part(at, output, _sources(at, result))
    elif isinstance(output, OutlineResult):
        outline, narration = await _outline(at, output)
    elif isinstance(output, ScatterResult):
        # The scatter map: published as scatter-map.json (working role - seats and
        # synthesis mount it), and RETURNED so the completion path can expand the wave around it -
        # the outline's hand-off, one lane over.
        outline = output
        await publish_scatter_map(at.frame, at.deps, output, fact=at.fact)
        narration = (
            f"Territory mapped - {len(output.seats)} seats toward {output.target_count} {output.entity}."
        )
    elif isinstance(output, RecordSet):
        outline, narration = await _records(at, output)
    elif isinstance(output, str) and output.strip() and publishes_text(at.spec, at.bots):
        narration = await _document(at, output, _sources(at, result))
    if narration is None:
        narration = narration_of(output)
    return narration, outline


def _ended(result: Any, override: Outcome | None) -> tuple[Outcome, str]:
    """The step's ending, typed: handed up in the typed slot for the ladder's status table. A
    `failed` one is the agent's verdict, a typed ending like `blocked`, unless a site wall stopped it."""
    ended = override or result.output
    if ended.status == "failed" and (died := wall_death(ended, result.all_messages())) is not None:
        raise died
    return ended, outcome_narration(ended)


# How each site wall opens a failed step's reason (`wall_death`).
_WALLS: Final = {
    FailureKind.SITE_UNREACHABLE: "the site ",
    FailureKind.SITE_REFUSES_BROWSER: "the site refused the computer's browser, so it ",
    FailureKind.SITE_CHALLENGED: "the site's page needs hosts this sandbox refuses (a bot check), so it ",
}


def wall_death(ended: Outcome, messages: list[Any]) -> ModelIncapable | None:
    """A `failed` ending that stopped at a site the computer cannot reach (`unreachable_target`), as
    the ladder's `final` death with that wall's kind and advice; None when no wall stopped it."""
    if (dead := unreachable_target(messages)) is None:
        return None
    kind = wall_kind(dead)
    why = "could not be reached from here, so another attempt cannot help"
    reason = f"{_WALLS[kind]}{why}: {ended.summary[:1000]}"
    return ModelIncapable(reason, pickup="continue", kind=kind)


async def _outline(at: Output, output: OutlineResult) -> tuple[OutlineResult | None, str]:
    spec = at.spec
    if spec.lane_role == "outline_draft":
        # A consensus PROPOSAL: published under its own name for the merge step to
        # mount - never as outline.md (that name is the merge's to claim) and never returned (only
        # the merge's outline may expand the plan).
        ref = spec.model_ref or at.step.id.hex[:8]
        filename = f"proposal-{re.sub(r'[^a-z0-9]+', '-', ref.lower()).strip('-')}.md"
        await publish_outline_result(
            at.frame,
            at.deps,
            output,
            fact=at.fact,
            filename=filename,
            title=(spec.contract.output.title if spec.contract and spec.contract.output else filename),
        )
        return None, f"Outline proposal ready - {len(output.sections)} sections ({ref})."
    # The outline step's typed outline: published as outline.md, and RETURNED so the
    # completion path can expand the plan around it - the fan-out's whole hand-off.
    await publish_outline_result(at.frame, at.deps, output, fact=at.fact)
    return output, (
        f"Outline ready - {len(output.sections)} sections, "
        f"{sum(len(s.figures) for s in output.sections)} figures."
    )


async def _records(at: Output, output: RecordSet) -> tuple[RecordSet, str]:
    """One seat's harvest: records-<seat>[-rN].jsonl through the one door (working role). The
    SPEC's own stamp names the seat - the system states its own fact, and a model echo (right or
    wrong) never outvotes it: exhaustion accounting and the mount filename both key off this
    field, and an echoed sibling id once fenced live ground out of every re-scatter."""
    named = output.model_copy(update={"seat": seat_of(at.spec)})
    await publish_record_set(at.frame, at.deps, named, round_no=at.spec.scatter_round or 1, fact=at.fact)
    found = len(named.records)
    return named, f"Gathered {found} record(s)" + (" - territory exhausted." if named.exhausted else ".")


async def _part(at: Output, output: SubmittedPart, sources: Mapping[str, str] | None) -> str:
    """One part of a long document, its `[S#]` marks kept for assembly's one render; its narration is its
    summary, which the parts after it read (`longform.part_tail`)."""
    await publish_document_text(
        at.frame, at.deps, at.spec, output.content, fact=at.fact, sources=sources, marked=True
    )
    return output.summary.strip()


def _sources(at: Output, result: Any) -> dict[str, str] | None:
    """The pages a citing agent (`AgentDef.cites`) may name, by source id: the task's and its run's; else None."""
    try:
        cites = definition_for(str(at.spec.agent), at.deps).cites
    except KeyError:
        return None
    return citable(at.deps.sources, result.all_messages()) if cites else None


def receipt_line(title: str, filename: str, words: int) -> str:
    """The publish line: the title, the file it is saved as, the length. A title that IS the file name
    (the writer headed the document with it) names the file once."""
    where = f"{words} words" if title == filename else f"`{filename}`, {words} words"
    return f"Published **{title}** ({where})."


async def _document(at: Output, output: str, sources: Mapping[str, str] | None) -> str:
    """A text deliverable, published as the document; its receipt is the narration."""
    title, filename = await publish_document_text(
        at.frame, at.deps, at.spec, output, fact=at.fact, sources=sources, marked=at.equipped.cited_later
    )
    words = len(output.split())
    narration = receipt_line(title, filename, words)
    if at.spec.lane_role == "synthesis":
        # The scatter finale IS the answer. Without this, a findings ask ended in seat
        # receipts + a Pending card and the list itself hid behind a click: the
        # document's head rides the thread, the publish line becomes its quiet
        # coda.
        return f"{synthesis_digest(output)}\n\n{narration}"
    return narration
