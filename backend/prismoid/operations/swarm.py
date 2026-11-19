# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Scatter - wide-and-deep research: its shape, its expansion and its iteration loop.

The Lead puts a scatter recipe on a researcher step that must find many items across the web; the
commit turns that step into ONE map step (`scatter_plan`), and the map's typed `ScatterResult` fans out
at its completion, because the honest width is unknowable before someone has looked at the territory.
The pure half (the shapes, `validate_scatter` with its legible retry gotcha `InvalidScatter`, `expand`,
`rescatter`, the canonical fold and `tally`) runs in the no-model simulator
(`tests/unit/test_swarm_policy.py`); the persistence half calls it from the step completion path:

* `expand_after_scatter` - the map step's typed map lands → N READY gather seats + one PENDING
  synthesis, in the caller's transaction, with the `PlanEdit(EXPAND)` record and the fresh
  `task.planned` snapshot; whatever waited on the map step now waits on the synthesis.
* `after_gather` - a seat settles → when its whole wave has settled, the TALLY: read every seat's
  published records from the artifacts (the wave's durable truth - a resumed workflow re-reads the
  same blobs), canonicalize + dedupe, compare against the map's count contract, and either let
  synthesis promote on what stands or RE-SCATTER the gap as a new READY wave whose edges land into
  synthesis BEFORE the settling seat's own promotion runs - which is what makes the loop race-free by
  construction.

**The iteration law** (the WANDR evidence: the field loses 47-89% of records in DISCOVERY, before
enrichment ever runs - one wave is structurally an undershoot): while coverage < the map's count
contract AND rounds remain AND un-exhausted ground remains, the plan RE-SCATTERS the gap - new seats
briefed with the shortfall and the exclusion list of everything already found. The loop is bounded
three ways: `swarm_max_rounds` (conf), every seat of the latest round reporting `exhausted`, and the
Bursar at every frontier. The tally is best-effort BY CONTRACT: any failure costs the re-scatter round,
never the task - synthesis proceeds on what stands and reconciles the shortfall honestly.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID

import logfire
import orjson
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid import storage
from prismoid.conf import settings
from prismoid.db.base import utcnow
from prismoid.db.models.artifact import Artifact
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.plan import PlanEdge as PlanEdgeRow
from prismoid.db.models.plan import PlanEdit as PlanEditRow
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.schemas.contract import Contract, Criteria
from prismoid.schemas.events import PlanEdited, TaskPlanned
from prismoid.schemas.ids import AgentName, ProjectId, StepId, TaskId
from prismoid.schemas.plan import PlanEdge, PlanEditKind, StepKind, StepSpec, StepState
from prismoid.schemas.plan import PlanEdit as PlanEditView
from prismoid.schemas.swarm import (
    SCATTER_MAX_SEATS,
    SCATTER_MIN_SEATS,
    SEAT_GOAL_MAX,
    RecordSet,
    ResearchRecord,
    ScatterResult,
    SeatBrief,
)

MAP_FILENAME = "scatter-map.jsonl"  # the lane's contract: seats and synthesis mount it by name

_SETTLED = frozenset({StepState.DONE, StepState.SKIPPED, StepState.ESCALATED})


class InvalidScatter(Exception):
    """The map cannot expand - the message IS the map step's retry gotcha."""


# ── the plan shapes ───────────────────────────────────────────────────────────────────────


def scatter_plan(
    goal: str, *, target_count: int | None = None
) -> tuple[list[tuple[StepId, StepSpec]], list[PlanEdge], Criteria]:
    """Phase 1: one map step (`target_count` when the recipe names one); the fan-out comes from its typed
    map at completion, because the honest width is unknowable before."""
    count = (
        [f"the count contract's target_count is {target_count}, the number of items the step must find"]
        if target_count is not None
        else []
    )
    spec = StepSpec(
        kind=StepKind.STEP,
        agent=AgentName("researcher"),
        lane_role="scatter_map",
        verify=False,
        artifact_role="working",
        # Titles carry the ROLE, not the goal: the goal is the page's own
        # user bubble and the seat brief's body - restating it per row buried the tree's
        # shape under truncated repetition (the owner's screenshot).
        title="Map the territory",
        contract=Contract(
            goal=goal,
            acceptance=[
                "the map divides the territory into DISJOINT seats - no two fences overlap",
                "the count contract matches the ask's own numbers",
                *count,
                "record_fields name exactly what each record must answer",
            ],
            output=None,  # the typed ScatterResult is the product; the system publishes the map
        ),
    )
    return (
        [(StepId(uuid.uuid4()), spec)],
        [],
        Criteria(must=[goal], tone="", audience=""),
    )


def validate_scatter(scatter: ScatterResult, *, max_width: int) -> None:
    """Code disposes. Every refusal names its fix - the retry gotcha, never a stuck plan."""
    width = len(scatter.seats)
    if width > min(max_width, SCATTER_MAX_SEATS):
        raise InvalidScatter(
            f"{width} seats is more than this host allows (at most {min(max_width, SCATTER_MAX_SEATS)}) "
            "- merge adjacent territories until the map fits; never ask to widen the cap"
        )
    if width < SCATTER_MIN_SEATS:
        raise InvalidScatter(
            f"{width} seat is not a scatter - divide the territory into at least {SCATTER_MIN_SEATS} "
            "disjoint seats, or this ask belonged to a single researcher step"
        )
    ids = [s.id for s in scatter.seats]
    if len(set(ids)) != len(ids):
        raise InvalidScatter("two seats share an id - renumber them g01, g02, … uniquely")
    fences = [s.not_here.strip().lower() for s in scatter.seats]
    if len(set(fences)) != len(fences):
        raise InvalidScatter(
            "two seats carry the SAME fence - each seat's not_here must name what its "
            "SIBLINGS own, so the fences are necessarily distinct"
        )
    goals = [s.goal.strip().lower() for s in scatter.seats]
    if len(set(goals)) != len(goals):
        raise InvalidScatter("two seats share a goal verbatim - the territories must be disjoint")


def _gather_spec(
    scatter: ScatterResult,
    seat: SeatBrief,
    *,
    round_no: int,
    exclusions: Sequence[str],
) -> StepSpec:
    fields = ", ".join(scatter.record_fields)
    exclusion_note = ""
    if exclusions:
        listed = "; ".join(list(exclusions)[:200])
        exclusion_note = (
            f"\nALREADY FOUND (round {round_no} re-scatter) - never re-submit these entities: {listed}"
        )
    round_mark = f" · round {round_no}" if round_no >= 2 else ""
    return StepSpec(
        kind=StepKind.STEP,
        agent=AgentName("researcher"),
        lane_role="gather",
        scatter_round=round_no,
        scatter_seat=seat.id,
        verify=False,
        artifact_role="working",
        tolerates_missing_inputs=True,  # the map mounts; a lost sibling never blocks a seat
        title=f"Gather {seat.id}{round_mark}",
        contract=Contract(
            goal=(
                f"{seat.goal}\n\nOne record = one {scatter.entity}. Each record MUST carry: "
                f"{fields}; the source id of the page it rests on; and 1–3 VERBATIM excerpts from that page that support "
                f"every answer field. NOT yours (a sibling owns it): {seat.not_here}."
                f"{exclusion_note}"
            ),
            acceptance=[
                "every record's excerpts are verbatim from the cited page and support every answer field",
                "no record belongs to a sibling's fence",
                "set exhausted=true honestly when the fence's territory holds no more",
            ],
            output=None,  # the typed RecordSet is the product; the system publishes records
        ),
    )


def synthesis_spec(scatter: ScatterResult, *, goal: str, section_total: int) -> StepSpec:
    return StepSpec(
        kind=StepKind.STEP,
        agent=AgentName("writer"),
        lane_role="synthesis",
        join="quorum",
        tolerates_missing_inputs=True,
        section_total=section_total,
        title="Synthesize the findings",
        contract=Contract(
            goal=(
                f"{goal}\n\nThe mounted scatter-map and per-seat record files hold the harvest. "
                "Produce the deliverable: canonicalize entities (collapse aliases, drop "
                "duplicates), present every record with its source, RECONCILE the count "
                "honestly against the map's target (a shortfall is stated, never padded), and "
                "report disagreement between seats' findings AS a finding."
            ),
            acceptance=[
                "every claim in the deliverable traces to a mounted record's URL",
                "the delivered count is stated against the target - shortfalls named, never padded",
                "duplicates and aliases are collapsed to one entity each",
            ],
            output=None,  # the writer's prose IS the deliverable (the text lane)
        ),
    )


def expand(
    scatter: ScatterResult,
    *,
    goal: str,
    map_step: StepId,
    max_width: int,
) -> tuple[list[tuple[StepId, StepSpec]], list[PlanEdge]]:
    """Round 1: the map to N gather seats plus one synthesis, as pure values;
    the synthesis edges from the map too."""
    validate_scatter(scatter, max_width=max_width)
    gathers = [
        (StepId(uuid.uuid4()), _gather_spec(scatter, seat, round_no=1, exclusions=()))
        for seat in scatter.seats
    ]
    synthesis_id = StepId(uuid.uuid4())
    synthesis = synthesis_spec(scatter, goal=goal, section_total=len(gathers))
    edges = [
        *(PlanEdge(from_step=map_step, to_step=gid, kind="produces_for") for gid, _ in gathers),
        PlanEdge(from_step=map_step, to_step=synthesis_id, kind="produces_for"),
        *(PlanEdge(from_step=gid, to_step=synthesis_id, kind="produces_for") for gid, _ in gathers),
    ]
    return [*gathers, (synthesis_id, synthesis)], edges


def rescatter(
    scatter: ScatterResult,
    *,
    round_no: int,
    gap: int,
    exclusions: Sequence[str],
    exhausted_fences: Sequence[str],
    max_width: int,
) -> list[tuple[StepId, StepSpec]]:
    """Round N >= 2, the gap wave: exhausted seats are not re-plowed, survivors are re-briefed with the
    shortfall and exclusions, and width shrinks with the gap."""
    live = [s for s in scatter.seats if s.id not in set(exhausted_fences)]
    if not live:
        return []
    width = max(1, min(len(live), max_width, -(-gap // 4)))  # ~4 entities per re-scatter seat
    picked = live[:width]
    rider = (
        f" - the harvest is {gap} short of the {scatter.target_count} target; "
        "hunt the misses: alternate phrasings, adjacent directories, non-obvious sources"
    )
    wave: list[tuple[StepId, StepSpec]] = []
    for seat in picked:
        # The SAME id as the round-1 fence - identity is the fence, the round is the spec's
        # own stamp, and the step row is unique by uuid. (Run 5 live: an `{id}r{n}` suffix
        # overflowed the 24-char cap on a 23-char map-written id and killed the whole round.)
        # The goal truncates so the rider FITS - SeatBrief re-validates max_length here, and
        # a near-cap round-1 goal once cost the whole gap round to string_too_long.
        rebriefed = SeatBrief(
            id=seat.id,
            goal=f"{seat.goal[: SEAT_GOAL_MAX - len(rider)]}{rider}",
            not_here=seat.not_here,
            expected=seat.expected,
        )
        wave.append(
            (StepId(uuid.uuid4()), _gather_spec(scatter, rebriefed, round_no=round_no, exclusions=exclusions))
        )
    return wave


# ── canonicalization + the tally (pure - the simulator drives exactly these) ──────────────

_LEGAL_SUFFIXES = re.compile(
    # a comma, or the FIRST blank of a run (`(?<!\s)`): retrying every blank inside a long
    # run was quadratic - the same strings fold, each from its leftmost start
    r"(?:,|(?<!\s))\s+(?:inc|incorporated|corp|corporation|co|company|ltd|limited|llc|plc|sa|ag|gmbh)\.?$",
    re.IGNORECASE,
)
_NOISE = re.compile(r"[^\w\s]+")  # Unicode \w on purpose - see canonicalize


def canonicalize(entity: str) -> str:
    """The identity fold: strip legal suffixes, fold accents (NFKD), casefold, drop punctuation, squeeze
    whitespace. Unicode-aware, so non-Latin names never fold to an empty key."""
    text = _LEGAL_SUFFIXES.sub("", entity.strip())
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = _NOISE.sub(" ", text.casefold())
    return " ".join(text.split())


def dedupe(records: Iterable[ResearchRecord]) -> list[ResearchRecord]:
    """First-wins per canonical entity; a name the fold cannot hold keys on its raw casefold, so a record
    is never silently dropped."""
    seen: set[str] = set()
    out: list[ResearchRecord] = []
    for record in records:
        key = canonicalize(record.entity) or record.entity.strip().casefold()
        if key and key not in seen:
            seen.add(key)
            out.append(record)
    return out


def records_filename(seat: str, round_no: int) -> str:
    """One seat-round's records file, round-suffixed from round 2: the synthesis mount is last-write-wins,
    so a re-scattered seat must never republish its round-1 path."""
    return f"records-{seat}.jsonl" if round_no <= 1 else f"records-{seat}-r{round_no}.jsonl"


@dataclass(frozen=True)
class Tally:
    """One wave's verdict, pure. `done` means synthesis proceeds on what stands."""

    unique: int
    target: int
    gap: int
    done: bool
    reason: str  # target-met | rounds-exhausted | ground-exhausted | continue


def tally(
    scatter: ScatterResult,
    records: Sequence[ResearchRecord],
    *,
    rounds_used: int,
    max_rounds: int,
    all_exhausted: bool,
) -> Tally:
    unique = len(dedupe(records))
    gap = max(0, scatter.target_count - unique)
    if gap == 0:
        return Tally(unique, scatter.target_count, 0, True, "target-met")
    if rounds_used >= max_rounds:
        return Tally(unique, scatter.target_count, gap, True, "rounds-exhausted")
    if all_exhausted:
        return Tally(unique, scatter.target_count, gap, True, "ground-exhausted")
    return Tally(unique, scatter.target_count, gap, False, "continue")


# ── the persistence half ─────────────────────────────────────────────────────────────────────


async def _snapshot(
    session: AsyncSession, task: Task, *, reason: str, round_no: int, payload: dict[str, object]
) -> None:
    """The expansion record: PlanEdit row + `plan.edited` + a fresh `task.planned` snapshot
    wearing the scatter route - the longform tail, with the routed record."""
    now = utcnow()
    session.add(
        PlanEditRow(
            task_id=task.id,
            kind=PlanEditKind.EXPAND.value,
            reason=reason[:2000],
            payload=payload,
        )
    )
    seq = await task.next_seq(session)
    await TaskEvent.append(
        session,
        PlanEdited(
            seq=seq,
            task_id=TaskId(task.id),
            project_id=ProjectId(task.project_id) if task.project_id else None,
            at=now,
            edit=PlanEditView(kind=PlanEditKind.EXPAND, reason=reason[:200], at=now),
        ),
    )
    from prismoid.operations.planning_steps import plan_view

    view = await plan_view(session, task)
    seq = await task.next_seq(session)
    await TaskEvent.append(
        session,
        TaskPlanned(
            seq=seq,
            task_id=TaskId(task.id),
            project_id=ProjectId(task.project_id) if task.project_id else None,
            at=utcnow(),
            plan=view,
            route="scatter",
            route_source="detector",
            route_why=f"round {round_no}: {reason[:150]}",
        ),
    )


async def expand_after_scatter(
    session: AsyncSession, task: Task, map_step: PlanStep, scatter: ScatterResult
) -> int:
    """The map → the round-1 wave + synthesis, in the caller's transaction; what waited on the map waits on
    the synthesis. Raises `InvalidScatter` (legible, the map step's retry gotcha); nothing here commits."""
    mapped = StepSpec.model_validate(map_step.spec).contract
    steps, edges = expand(
        scatter,
        goal=mapped.goal if mapped is not None else task.goal,
        map_step=StepId(map_step.id),
        max_width=settings.worker.swarm_max_width,
    )

    # same layer; the one persist path - and the one pin law: the user's requested
    # skills ride every wave step, not only the map
    from prismoid.operations.planning_steps import hand_downstream, pin_skills, write_steps

    existing = await PlanStep.for_task(session, task.id)
    base = max((s.ordinal for s in existing), default=-1) + 1
    rows = await write_steps(session, task, pin_skills(steps, task), edges, base_ordinal=base)
    await hand_downstream(session, task.id, map_step.id, rows)

    # `write_steps` births edge-targeted steps PENDING - right for a fresh plan, wrong here:
    # the seats' only upstream is the map step COMPLETING RIGHT NOW, so promotion already
    # happened, structurally. Flip them READY; synthesis stays PENDING on its seats.
    for row in rows:
        if StepSpec.model_validate(row.spec).lane_role == "gather":
            row.state = StepState.READY.value

    await _snapshot(
        session,
        task,
        reason=f"scatter map landed - {len(scatter.seats)} seats toward {scatter.target_count} {scatter.entity}",
        round_no=1,
        payload={"round": 1, "scatter": scatter.model_dump(mode="json")},
    )
    return len(rows)


async def _read_blob(sha256: str) -> bytes | None:
    try:
        return await storage.aget(sha256)
    except Exception as exc:
        logfire.warn(
            "swarm: blob {sha} unreadable ({kind}) - skipped", sha=sha256[:12], kind=type(exc).__name__
        )
        return None


@dataclass(frozen=True)
class _WaveState:
    """One settled wave, read whole: the count contract, its graph anchors, and the harvest."""

    scatter: ScatterResult
    map_step: StepId
    synthesis: PlanStep | None
    records: list[ResearchRecord]
    exhausted: set[str]  # FENCE ids, keyed off each seat spec's own stamp - never a blob echo
    rounds_used: int
    next_ordinal: int


async def _harvest_state(session: AsyncSession, task: Task, step: PlanStep) -> _WaveState | None:
    """The settling seat's WAVE - derived from the graph, read from the published artifacts.

    EDGE-SCOPED, never task-pooled: the wave is exactly the steps wired from this seat's own
    map step, so a follow-up turn's scatter (its own map) and this one stay structurally
    disjoint - pooling every gather step of the task once tallied turn 2 against turn 1's
    map, records, and rounds. Returns None while the wave is still
    settling, when the seat has no map edge, or when the settled wave's map is unreadable
    (logged LOUD - the one place a skipped tally and target-met could look alike).

    ORDER IS THE LOCK LAW (`longform.py`): the caller holds the task row's seq lock, so the
    cheap step/edge reads come first and every artifact/blob read waits behind the
    wave-settled gate - a non-final settle does zero I/O. The final settle's ≤ width+1 blob
    reads run inside the lock DELIBERATELY: the re-scatter wave must land before this seat's
    completion promotes synthesis (the race-free construction), and that bounded read is
    the price of the race not existing.
    """
    steps = await PlanStep.for_task(session, task.id)
    edges = await PlanEdgeRow.for_task(session, task.id)
    specs = {s.id: StepSpec.model_validate(s.spec) for s in steps}

    map_step_id = seat_map_step(edges, specs, step.id)
    if map_step_id is None:
        logfire.warn("scatter tally skipped: seat {s} carries no map edge", s=str(step.id))
        return None

    gathers, synthesis = wave_members(steps, edges, specs, map_step_id)
    if not wave_settled(gathers):
        return None  # still settling - a later seat's own completion re-asks
    rounds_used = max((sp.scatter_round or 1 for _, sp in gathers), default=1)
    next_ordinal = max((s.ordinal for s in steps), default=-1) + 1

    # newest readable map wins: a retried map step tallies on its ACCEPTED attempt
    scatter = await _newest_dataset(
        session, map_step_id, ScatterResult, "swarm: map {a} is not a scatter result ({kind})"
    )
    if scatter is None:
        logfire.error(
            "scatter tally SKIPPED for task {t}: wave settled but no readable map on step {m} - "
            "synthesis proceeds on what stands (this line is what distinguishes that from target-met)",
            t=str(task.id),
            m=str(map_step_id),
        )
        return None

    records, exhausted = await _harvest_records(session, gathers)
    return _WaveState(scatter, StepId(map_step_id), synthesis, records, exhausted, rounds_used, next_ordinal)


def seat_map_step(edges: Sequence[PlanEdgeRow], specs: Mapping[UUID, StepSpec], seat_id: UUID) -> UUID | None:
    """The map step a gather seat is wired from (its first `scatter_map` upstream), or None when
    the seat carries no map edge."""
    map_ids = [
        e.from_step_id
        for e in edges
        if e.to_step_id == seat_id
        and (msp := specs.get(e.from_step_id)) is not None
        and msp.lane_role == "scatter_map"
    ]
    return map_ids[0] if map_ids else None


def wave_members(
    steps: Sequence[PlanStep], edges: Sequence[PlanEdgeRow], specs: Mapping[UUID, StepSpec], map_step_id: UUID
) -> tuple[list[tuple[PlanStep, StepSpec]], PlanStep | None]:
    """The wave wired from one map step: its gather seats (with their specs) and its synthesis.
    Edge-scoped on purpose, so another turn's scatter never pools into this one."""
    rows = {s.id: s for s in steps}
    wave_ids = {e.to_step_id for e in edges if e.from_step_id == map_step_id}
    gathers = [(rows[i], specs[i]) for i in wave_ids if i in rows and specs[i].lane_role == "gather"]
    synthesis = next((rows[i] for i in wave_ids if i in rows and specs[i].lane_role == "synthesis"), None)
    return gathers, synthesis


def wave_settled(gathers: Sequence[tuple[PlanStep, StepSpec]]) -> bool:
    """True once the wave has seats and every one of them has settled."""
    return bool(gathers) and all(r.step_state in _SETTLED for r, _ in gathers)


async def _newest_dataset[M: (ScatterResult, RecordSet)](
    session: AsyncSession, step_id: UUID, model: type[M], not_a_model: str
) -> M | None:
    """The newest of a step's dataset artifacts that reads and parses as `model`, or None. An
    older one is tried when a newer is a rejected attempt's output or corrupt bytes, so a laddered
    retry that republished is read on its accepted attempt and a bad blob never kills the tally."""
    for artifact in reversed(await Artifact.for_step(session, step_id)):
        if artifact.type != "dataset":
            continue
        raw = await _read_blob(artifact.sha256)
        if raw is None:
            continue
        try:
            return model.model_validate(orjson.loads(raw))
        except Exception as exc:
            # a rejected attempt's output, or corrupt bytes - try the older one
            logfire.info(not_a_model, a=str(artifact.id), kind=type(exc).__name__)
    return None


async def _harvest_records(
    session: AsyncSession, gathers: Sequence[tuple[PlanStep, StepSpec]]
) -> tuple[list[ResearchRecord], set[str]]:
    """Every DONE seat's newest readable record set, in seat order, and the fences whose seats
    reported their ground exhausted."""
    records: list[ResearchRecord] = []
    exhausted: set[str] = set()
    for row, sp in gathers:
        if row.step_state is not StepState.DONE:
            continue
        # newest readable harvest wins (a laddered retry republishes); not a RecordSet (or
        # unreadable) is never the tally's death
        record_set = await _newest_dataset(
            session, row.id, RecordSet, "swarm: dataset {a} is not a record set ({kind})"
        )
        if record_set is None:
            continue
        records.extend(record_set.records)
        if record_set.exhausted and sp.scatter_seat:
            # the SPEC's own stamp, never the blob's `seat` echo - a model echoing a
            # sibling's id could fence out live ground or make ground-exhausted
            # unreachable
            exhausted.add(sp.scatter_seat)
    return records, exhausted


async def after_gather(session: AsyncSession, task: Task, step: PlanStep) -> None:
    """The iteration law's hook - called as each seat settles, acts only when the wave has.

    BEST-EFFORT BY CONTRACT, and the SAVEPOINT is what enforces it: everything the tally
    writes rides `begin_nested()`, so any failure - DB or not - rolls back to the savepoint
    and the settling seat's own completion (its DONE/SKIPPED, its events, its promotion)
    commits untouched. Without it the two failure arms each inverted the contract: an
    aborted session failed the task one seat short of synthesis, and a post-`write_steps`
    crash committed a half-recorded wave with no PlanEdit on the append-only stream.
    """
    try:
        async with session.begin_nested():
            state = await _harvest_state(session, task, step)
            if state is None:
                return
            scatter, exhausted, rounds_used = state.scatter, state.exhausted, state.rounds_used
            all_fences = {s.id for s in scatter.seats}
            verdict = tally(
                scatter,
                state.records,
                rounds_used=rounds_used,
                max_rounds=settings.worker.swarm_max_rounds,
                all_exhausted=bool(all_fences) and all_fences <= exhausted,
            )
            logfire.info(
                "scatter tally: {u}/{t} unique after round {r} - {reason}",
                u=verdict.unique,
                t=verdict.target,
                r=rounds_used,
                reason=verdict.reason,
            )
            if verdict.done:
                return  # synthesis promotes naturally at this seat's own completion

            exclusions = sorted({canonicalize(r.entity) for r in state.records if r.entity})
            wave = rescatter(
                scatter,
                round_no=rounds_used + 1,
                gap=verdict.gap,
                exclusions=exclusions,
                exhausted_fences=sorted(exhausted),
                max_width=settings.worker.swarm_max_width,
            )
            if not wave or state.synthesis is None:
                return
            edges = [
                # the map edges the gap seats too - round 1's own wiring: the mounts derive
                # /task/inputs from incoming edges, and the seat's craft promises the
                # map as orientation in EVERY round
                *(PlanEdge(from_step=state.map_step, to_step=gid, kind="produces_for") for gid, _ in wave),
                *(
                    PlanEdge(from_step=gid, to_step=StepId(state.synthesis.id), kind="produces_for")
                    for gid, _ in wave
                ),
            ]

            # same layer; the one persist path - and the one pin law for the gap wave
            from prismoid.operations.planning_steps import pin_skills, write_steps

            rows = await write_steps(
                session, task, pin_skills(list(wave), task), edges, base_ordinal=state.next_ordinal
            )
            for row in rows:
                if StepSpec.model_validate(row.spec).lane_role == "gather":
                    row.state = StepState.READY.value  # its upstreams (this wave, the DONE map) settled

            await _snapshot(
                session,
                task,
                reason=(
                    f"re-scatter: {verdict.unique}/{verdict.target} unique - "
                    f"{len(rows)} gap seat(s) for the {verdict.gap} missing"
                ),
                round_no=rounds_used + 1,
                payload={"round": rounds_used + 1, "gap": verdict.gap, "seats": [str(g) for g, _ in wave]},
            )
    except Exception:
        logfire.exception(
            "scatter tally failed for task {t}; the round is lost, synthesis proceeds on what stands",
            t=str(task.id),
        )
