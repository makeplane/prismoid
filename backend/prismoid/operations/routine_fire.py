# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Routines - the fire gate. A Routine executes nothing: `execute_fire` is the ONE policy gate
every source converges on (clock sweep, manual Run-now, the API door, the event matcher), and the
only thing a fire ever does is call `create_task`.

The gate re-reads the routine FOR UPDATE and walks its stages in order (`_fire_gate`); the first
stage to answer ends the fire with a journaled receipt, and a fire every stage lets through is
minted. The pure checks each stage asks live in `routine_gate.py`; the receipt and the clock
helpers in `routine_events.py`. See `routines.py` for the three idempotent layers.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import logfire
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.conf import settings
from prismoid.db.models.actor import Actor
from prismoid.db.models.routine import Routine, RoutineRun
from prismoid.db.models.task import Task
from prismoid.db.models.workspace import WorkspaceMember
from prismoid.errors import AllowanceSpent, PrismoidError, ValidationFailed
from prismoid.operations import allowance, routine_gate, steering
from prismoid.operations import inbox as inbox_ops
from prismoid.operations import tasks as tasks_ops
from prismoid.operations import usage as usage_ops
from prismoid.operations.routine_events import (
    advance_past,
    auto_pause,
    fire_failed,
    jitter,
    journal,
    result_of,
    spec_of,
)
from prismoid.operations.routine_gate import OverlapMove, Refusal
from prismoid.schemas.inputs import RawInput
from prismoid.schemas.routines import (
    GOAL_CAP,
    PAYLOAD_LAW,
    CatchUp,
    FireResult,
    PausedReason,
    RoutineStatus,
    RoutineTemplate,
    RunDecision,
    RunSource,
    ScheduleSpec,
    TriggerKind,
    day_start,
    mint_client_token,
    missed_fires,
    month_start,
    next_day_start,
    next_fire_at,
)


async def execute_fire(
    session: AsyncSession,
    routine_id: uuid.UUID,
    *,
    occurrence: str,
    source: RunSource,
    payload: bytes | None = None,
    chain_depth: int = 0,
) -> FireResult:
    """The ONE gate. Callers hold no locks and pass no state - the routine is re-read FOR
    UPDATE and judged as it is NOW (the edit race closes here). Two transactions when a task
    is minted (`create_task` owns and commits the first; the receipt + advance are the
    second), and every step of both is idempotent under replay - see the module docstring.

    Every exit journals its decision, with two deliberate exceptions: the kill switch DEFERS
    (unjournaled + unadvanced, so the occurrence re-offers after re-enable - a receipt would
    consume it and suppress the fire forever), and a still-waiting park returns BUFFERED for
    the sweep's drain arm to re-offer next tick. An unexpected exception is journaled ERROR in
    a fresh transaction with the clock advanced past the occurrence - a poison fire must never
    wedge the schedule.
    """
    if not settings.routines.enabled:
        return FireResult(run_id=None, decision=RunDecision.ERROR, detail="routines are disabled")
    try:
        return await _fire_gate(
            session,
            routine_id,
            occurrence=occurrence,
            source=source,
            payload=payload,
            chain_depth=chain_depth,
        )
    except Exception as exc:
        # The session that blew up may sit in an aborted transaction - judgment restarts clean.
        await session.rollback()
        logfire.error(
            "routine {rid} fire {occ} crashed: {err}",
            rid=str(routine_id),
            occ=occurrence,
            err=f"{type(exc).__name__}: {exc}",
        )
        relocked = await Routine.lock(session, routine_id)
        if relocked is None:
            return FireResult(run_id=None, decision=RunDecision.ERROR, detail=str(exc)[:400])
        fire_instant = routine_gate.fire_instant_of(occurrence)
        advance_past(
            relocked,
            routine_gate.schedule_of(relocked.trigger_kind, relocked.trigger),
            fire_instant,
            await Routine.clock(session),
        )
        # A crash is a failure like a refusal: it walks the ladder (a poison fire pauses instead of
        # erroring every tick forever) and the first of a streak knocks the owner.
        await fire_failed(session, relocked, why=f"{type(exc).__name__}: {str(exc)[:300]}")
        return await journal(
            session,
            relocked,
            occurrence=occurrence,
            source=source,
            decision=RunDecision.ERROR,
            scheduled_for=fire_instant,
            detail=f"{type(exc).__name__}: {str(exc)[:300]}",
            chain_depth=chain_depth,
        )


@dataclass(slots=True)
class _Fire:
    """One fire on its way through the gate, under the routine's row lock; the stages fill it in.
    `manual_on_paused` is Run-now on a routine that was PAUSED when the fire first locked it."""

    routine: Routine
    occurrence: str
    source: RunSource
    payload: bytes | None
    chain_depth: int
    now: dt.datetime
    scheduled_for: dt.datetime
    spec: ScheduleSpec | None = None
    fire_instant: dt.datetime | None = None
    is_parked: bool = False
    detail: str = ""
    owner: Actor | None = None
    manual_on_paused: bool = False


async def _fire_gate(
    session: AsyncSession,
    routine_id: uuid.UUID,
    *,
    occurrence: str,
    source: RunSource,
    payload: bytes | None,
    chain_depth: int,
) -> FireResult:
    """The gate's stages in order; the first to answer ends the fire, and a fire every stage lets
    through is minted. Every answer but the parked fire's BUFFERED is a journaled receipt."""
    locked = await _lock_unreceipted(session, routine_id, occurrence)
    if isinstance(locked, FireResult):
        return locked
    now = await Routine.clock(session)
    fire = _Fire(
        routine=locked,
        occurrence=occurrence,
        source=source,
        payload=payload,
        chain_depth=chain_depth,
        now=now,
        scheduled_for=now,
        manual_on_paused=source == RunSource.MANUAL and locked.status == RoutineStatus.PAUSED,
    )
    for stage in (
        _recover_own_mint,
        _refuse_stopped,
        _place_on_clock,
        _retire_out_of_season,
        _handle_missed,
        _refuse_without_owner,
        _refuse_without_allowance,
        _policy_stack,
        _overlap,
    ):
        if (result := await stage(session, fire)) is not None:
            return result
    return await _mint(session, fire)


async def _lock_unreceipted(
    session: AsyncSession, routine_id: uuid.UUID, occurrence: str
) -> Routine | FireResult:
    """Take the routine's row lock (left held) and return the row - or, when this occurrence
    already has a receipt, that receipt (Idempotency-Key semantics). The receipt is read cheaply
    before the lock and again UNDER it: two same-occurrence fires (an Idempotency-Key replay
    racing itself, a drain twin) both pass the unlocked read; the loser must answer with the
    winner's receipt, never judge the world a second time."""
    existing = await RoutineRun.by_occurrence(session, routine_id, occurrence)
    if existing is not None:
        return result_of(existing)
    routine = await Routine.lock(session, routine_id)
    if routine is None:
        return FireResult(run_id=None, decision=RunDecision.ERROR, detail="routine is gone")
    existing = await RoutineRun.by_occurrence(session, routine_id, occurrence)
    if existing is not None:
        return result_of(existing)
    return routine


async def _refuse(
    session: AsyncSession, fire: _Fire, refusal: Refusal, *, scheduled_for: dt.datetime | None
) -> FireResult:
    """Journal a non-fire with its reason (commits, releasing the row lock)."""
    return await journal(
        session,
        fire.routine,
        occurrence=fire.occurrence,
        source=fire.source,
        decision=refusal.decision,
        scheduled_for=scheduled_for,
        detail=refusal.detail,
        chain_depth=fire.chain_depth,
    )


async def _recover_own_mint(session: AsyncSession, fire: _Fire) -> FireResult | None:
    """Crash-resume / racing-twin recovery: if THIS occurrence already minted its task
    (`create_task` committed; the receipt transaction was lost), write the receipt it is owed.
    Without it the resumed gate would fall through to the overlap check, see its OWN task as
    "the previous run", and journal `skipped_overlap` while the task ran and spent unrecorded."""
    routine = fire.routine
    own = await Task.by_client_token(
        session, routine.owner_actor_id, mint_client_token(routine.id, fire.occurrence)
    )
    if own is None:
        return None
    return await _mint_receipt(
        session,
        routine,
        occurrence=fire.occurrence,
        source=fire.source,
        task=own,
        now=fire.now,
        scheduled_for=routine_gate.fire_instant_of(fire.occurrence) or fire.now,
        detail="recovered - the task was minted before a crash cut the receipt",
        chain_depth=fire.chain_depth,
    )


async def _refuse_stopped(session: AsyncSession, fire: _Fire) -> FireResult | None:
    """A paused or retired routine, and a chain past its depth cap, never fire - journaled."""
    halt = routine_gate.stopped(fire.routine.status, manual_on_paused=fire.manual_on_paused)
    if halt is not None:
        return await _refuse(session, fire, halt, scheduled_for=routine_gate.fire_instant_of(fire.occurrence))
    deep = routine_gate.chain_too_deep(fire.chain_depth, settings.routines.max_chain_depth)
    if deep is not None:
        return await _refuse(session, fire, deep, scheduled_for=None)
    return None


async def _place_on_clock(session: AsyncSession, fire: _Fire) -> FireResult | None:
    """Find the fire's scheduled instant. A PARKED fire (overlap=queue) re-entering the gate
    already passed the whole policy stack when it took the buffer slot, its lateness is
    deliberate (it was waiting, not missed), and `next_fire_at` was advanced at parking (so the
    superseded check would wrongly kill it): it re-derives its instant from its own occurrence and
    goes straight to the overlap stage, which owns expiry and the still-waiting park. Any other
    clock fire must still be the row's next fire."""
    routine = fire.routine
    fire.spec = spec_of(routine) if routine.trigger_kind == TriggerKind.SCHEDULE else None
    fire.is_parked = routine.buffered_occurrence == fire.occurrence
    if fire.source != RunSource.CLOCK:
        return None
    if fire.is_parked:
        fire.fire_instant = dt.datetime.fromisoformat(fire.occurrence)
        fire.scheduled_for = fire.fire_instant
        return None
    assert fire.spec is not None
    moved = routine_gate.schedule_moved(routine.next_fire_at, fire.occurrence)
    if moved is not None:
        return await _refuse(session, fire, moved, scheduled_for=None)
    assert routine.next_fire_at is not None  # `schedule_moved` refuses a missing one
    fire.fire_instant = routine.next_fire_at
    fire.scheduled_for = fire.fire_instant
    return None


async def _retire_out_of_season(session: AsyncSession, fire: _Fire) -> FireResult | None:
    """Seasons: standing is not eternal - whatever the source (event and token fires once slipped
    this check, so a max_runs season minted forever). Before the policy stack, so a lapsed
    routine's next touch is its honest, journaled retirement."""
    routine = fire.routine
    why = routine_gate.season_over(
        until_at=routine.until_at, max_runs=routine.max_runs, runs_minted=routine.runs_minted, now=fire.now
    )
    if why is None:
        return None
    return await _retire_at_gate(
        session,
        routine,
        fire.occurrence,
        fire.source,
        why,
        scheduled_for=fire.scheduled_for,
        chain_depth=fire.chain_depth,
    )


async def _handle_missed(session: AsyncSession, fire: _Fire) -> FireResult | None:
    """Misses: a clock fire older than the grace window fell while nothing was running. `skip`
    journals and moves on; `once` mints ONE coalesced make-up for the most recent miss (systemd
    Persistent=; Claude Desktop's stated policy). Never replay-all."""
    routine, spec, fire_instant, now = fire.routine, fire.spec, fire.fire_instant, fire.now
    if fire.source != RunSource.CLOCK or fire.is_parked:
        return None
    assert spec is not None and fire_instant is not None
    if not routine_gate.missed(fire_instant, now, grace_s=settings.routines.miss_grace_s):
        return None
    count, latest = missed_fires(spec, occurrence=fire_instant, now=now, jitter_s=jitter(routine.id))
    if routine.catch_up != CatchUp.SKIP:
        fire.scheduled_for = latest
        fire.detail = f"coalesced {count} missed fire(s) into one catch-up (catch_up=once)"
        return None
    advance_past(routine, spec, fire_instant, now)
    if spec.is_one_shot:
        # A missed one-shot is dead, and the owner hears it from us, not from the silence - the
        # season retirement's knock, same voice.
        routine.status = RoutineStatus.RETIRED.value
        await inbox_ops.routine_retired(
            session,
            actor_id=routine.owner_actor_id,
            routine_id=routine.id,
            routine_name=routine.name,
            why=(
                "Retired - its one-shot moment passed while nothing was running "
                "(beyond the grace window), so the run was skipped. "
                "Its run history stays readable."
            ),
        )
    detail = f"missed {count} fire(s) while nothing was running (catch_up=skip)"
    return await _refuse(
        session, fire, Refusal(RunDecision.SKIPPED_MISSED, detail), scheduled_for=fire_instant
    )


async def _refuse_without_owner(session: AsyncSession, fire: _Fire) -> FireResult | None:
    """The owner mints. A suspended or erased owner pauses the routine - unattended spend never
    outlives its accountable human. No knock: the account that would read it is gone. A
    removed member's routines do not keep spending in the workspace that removed them:
    the owner must still belong to the routine's workspace, or the routine pauses the same way."""
    routine = fire.routine
    owner = await session.get(Actor, routine.owner_actor_id)
    if owner is None or not owner.is_human() or not owner.is_active():
        note, refusal = (
            "the owner's account is suspended or erased",
            Refusal(RunDecision.ERROR, "owner unavailable - routine paused"),
        )
    elif await WorkspaceMember.get(session, routine.workspace_id, routine.owner_actor_id) is None:
        note, refusal = (
            "the owner is no longer a member of this workspace",
            Refusal(
                RunDecision.SKIPPED_MEMBERSHIP, "owner is not a member of the workspace - routine paused"
            ),
        )
    else:
        fire.owner = owner
        return None
    routine.status = RoutineStatus.PAUSED.value
    routine.paused_reason = PausedReason.OWNER.value
    routine.pause_note = note
    return await _refuse(session, fire, refusal, scheduled_for=fire.scheduled_for)


async def _refuse_without_allowance(session: AsyncSession, fire: _Fire) -> FireResult | None:
    """A workspace that has spent its allowance mints nothing (`allowance.py`, ADR-103) - whatever
    the source, a parked fire included. The fire is journaled (`skipped_allowance`)
    and the clock moves on; the routine stays active, because a spent allowance is the workspace's
    state, not the routine's fault: it fires again the first occurrence after the allowance is
    back. No knock and no pause - a pause would need a person to undo what the allowance coming
    back undoes by itself."""
    routine = fire.routine
    try:
        await allowance.require(session, routine.workspace_id)
    except AllowanceSpent as exc:
        if routine.buffered_occurrence == fire.occurrence:
            routine.buffered_occurrence = None  # a parked fire frees its slot, or "buffer full" lies
        advance_past(routine, fire.spec, fire.fire_instant, fire.now)
        return await _refuse(
            session,
            fire,
            Refusal(RunDecision.SKIPPED_ALLOWANCE, exc.message),
            scheduled_for=fire.scheduled_for,
        )
    return None


async def _policy_stack(session: AsyncSession, fire: _Fire) -> FireResult | None:
    """The policy stack, in order: throttle → daily cap → monthly cap
    (overlap is its own stage). MANUAL is exempt - the play button is user-caused, explicit, and
    editor-gated (the Apple/Devin rule); the stack defends against runaway CLOCKS. A parked fire
    skips it (it paid the stack when it parked). Advances are `advance_past`: CLOCK fires only -
    a token/API fire never moves the schedule."""
    if fire.source == RunSource.MANUAL or fire.is_parked:
        return None
    routine, spec = fire.routine, fire.spec
    floor = routine_gate.throttled(routine.last_fired_at, routine.min_interval_s, fire.now)
    if floor is not None:
        advance_past(routine, spec, fire.fire_instant, fire.now)
        return await _refuse(session, fire, floor, scheduled_for=fire.scheduled_for)
    tz_name = spec.timezone if spec is not None else "UTC"
    return await _refuse_over_daily_cap(session, fire, tz_name) or await _refuse_over_monthly_cap(
        session, fire, tz_name
    )


async def _refuse_over_daily_cap(session: AsyncSession, fire: _Fire, tz_name: str) -> FireResult | None:
    """The daily fire cap - counted in the ROUTINE'S OWN day (a cap that resets at someone else's
    midnight reads as broken). One-shots are exempt (the Claude rule). On breach the clock
    advances straight to tomorrow's first natural fire: one journal row, then silence until the
    day turns - never a row per minute."""
    routine, spec, now = fire.routine, fire.spec, fire.now
    if spec is not None and spec.is_one_shot:
        return None
    fired_today = await RoutineRun.minted_count_since(session, routine.id, since=day_start(tz_name, now))
    if fired_today < routine.daily_fire_cap:
        return None
    if spec is not None and fire.fire_instant is not None:
        resume_at = next_fire_at(
            spec,
            after=next_day_start(tz_name, now) - dt.timedelta(seconds=1),
            jitter_s=jitter(routine.id),
        )
        routine.advance(occurrence_fire_at=fire.fire_instant, to=resume_at)
    detail = f"daily cap of {routine.daily_fire_cap} reached - next fire after midnight {tz_name}"
    return await _refuse(
        session, fire, Refusal(RunDecision.SKIPPED_CAP, detail), scheduled_for=fire.scheduled_for
    )


async def _refuse_over_monthly_cap(session: AsyncSession, fire: _Fire, tz_name: str) -> FireResult | None:
    """The monthly money cap - the ledger join, month-to-date in the routine's calendar.
    Breach pauses (Make's budget-exhausted-deactivates, done in dollars); the pause note carries
    the resume, and the pause itself always notifies."""
    routine = fire.routine
    if routine.monthly_cap_usd is None:
        return None
    mtd = await usage_ops.routine_month_to_date(session, routine.id, since=month_start(tz_name, fire.now))
    if mtd < routine.monthly_cap_usd:
        return None
    note = f"monthly cap ${routine.monthly_cap_usd} reached (spent ${mtd:.2f})"
    await auto_pause(session, routine, reason=PausedReason.BUDGET, note=note)
    return await _refuse(
        session, fire, Refusal(RunDecision.SKIPPED_BUDGET, note), scheduled_for=fire.scheduled_for
    )


async def _overlap(session: AsyncSession, fire: _Fire) -> FireResult | None:
    """Overlap - its own stage, because a PARKED fire skips the policy stack but must re-enter
    HERE (expiry and the still-waiting park live in the queue moves). The previous run is still
    un-settled (HELD counts: approvals must not pile). MANUAL is exempt."""
    if fire.source == RunSource.MANUAL:
        return None
    routine, spec, fire_instant, now = fire.routine, fire.spec, fire.fire_instant, fire.now
    live = await Task.live_for_routine(session, routine.id)
    if live is None:
        return None
    move = routine_gate.overlap_move(
        routine.overlap,
        queueable=spec is not None and fire_instant is not None,
        is_parked=fire.is_parked,
        buffer_taken=routine.buffered_occurrence is not None,
        waited_s=(now - fire_instant).total_seconds() if fire_instant is not None else 0.0,
        buffer_max_s=settings.routines.queue_buffer_max_s,
    )
    if move is OverlapMove.PARK_EXPIRED:
        routine.buffered_occurrence = None
        detail = f"buffered fire expired after {settings.routines.queue_buffer_max_s}s (queue holds one)"
        return await _refuse(
            session, fire, Refusal(RunDecision.SKIPPED_OVERLAP, detail), scheduled_for=fire_instant
        )
    if move is OverlapMove.PARK_WAITING:
        # Still waiting: release the lock, change nothing - the drain arm re-offers this
        # occurrence next tick, so no workflow sleeps on a slot.
        await session.commit()
        return FireResult(run_id=None, decision=RunDecision.BUFFERED, detail="waiting")
    if move is OverlapMove.BUFFER_FULL:
        advance_past(routine, spec, fire_instant, now)
        detail = "buffer full - one fire is already waiting (queue holds one)"
        return await _refuse(
            session, fire, Refusal(RunDecision.SKIPPED_OVERLAP, detail), scheduled_for=fire_instant
        )
    if move is OverlapMove.TAKE_SLOT:
        # First arrival: take the buffer slot and ADVANCE - the sweep must stop re-offering this
        # occurrence on the due arm; the drain arm owns it now.
        routine.buffered_occurrence = fire.occurrence
        advance_past(routine, spec, fire_instant, now)
        await session.commit()
        return FireResult(run_id=None, decision=RunDecision.BUFFERED, detail="buffered")
    if move is OverlapMove.REPLACE:
        return await _replace_live(session, fire, live.id)
    # skip - the default; queue on a non-clock fire lands here too, honestly
    if routine.buffered_occurrence == fire.occurrence:
        # A park re-entering after overlap was edited off queue: free the slot, or "buffer full"
        # lies forever once queue is switched back.
        routine.buffered_occurrence = None
    advance_past(routine, spec, fire_instant, now)
    detail = f"the previous run is still going ({live.id})"
    return await _refuse(
        session, fire, Refusal(RunDecision.SKIPPED_OVERLAP, detail), scheduled_for=fire.scheduled_for
    )


async def _replace_live(session: AsyncSession, fire: _Fire, live_id: uuid.UUID) -> FireResult | None:
    """overlap=replace: cancel the live run, then let the fire go on to mint (None), re-judged.

    Cancels WITHOUT the routine lock: deliver/fail write the task row and then lock the routine
    (`on_settled`) - task→routine; cancelling under our routine→task order was an AB-BA deadlock,
    and `cancel_task_row`'s internal commit would drop our lock mid-gate anyway, leaving the rest
    of the gate to write an unlocked row. So: release, cancel in its own transaction, re-lock
    (left held), re-read the receipt and the status."""
    routine_id = fire.routine.id
    await session.commit()
    live = await session.get(Task, live_id)
    if live is not None and not live.is_terminal():
        assert fire.owner is not None  # the owner stage ran first
        # The routine's policy cancels, not the owner's hand: no human actor on the record.
        await steering.cancel_task_row(session, live, fire.owner, attribute=False)  # commits, wakes
    relocked = await Routine.lock(session, routine_id)
    if relocked is None:
        return FireResult(run_id=None, decision=RunDecision.ERROR, detail="routine is gone")
    fire.routine = relocked
    existing = await RoutineRun.by_occurrence(session, routine_id, fire.occurrence)
    if existing is not None:
        return result_of(existing)
    halt = routine_gate.stopped(relocked.status, manual_on_paused=fire.manual_on_paused)
    if halt is not None:
        return await _refuse(session, fire, halt, scheduled_for=routine_gate.fire_instant_of(fire.occurrence))
    fire.detail = routine_gate.joined(fire.detail, f"replaced the previous run ({live_id})")
    return None


async def _mint(session: AsyncSession, fire: _Fire) -> FireResult:
    """Mint the task through `create_task` (it owns and commits the first transaction), then write
    the receipt and advance the clock in a second. A crash between the two re-offers the
    occurrence, and `_recover_own_mint` writes exactly this receipt on re-entry. A refused mint is
    a journaled ERROR that counts toward the auto-pause ladder."""
    routine, spec, owner = fire.routine, fire.spec, fire.owner
    assert owner is not None  # the owner stage ran first
    # A buffered fire that made it here has the floor: clear the slot before minting.
    if routine.buffered_occurrence == fire.occurrence:
        routine.buffered_occurrence = None
        fire.detail = routine_gate.joined(fire.detail, "ran after buffering behind the previous run")

    template = RoutineTemplate.model_validate(routine.template)
    tz = ZoneInfo(spec.timezone) if spec is not None else dt.UTC
    title = f"{routine.name} · {fire.scheduled_for.astimezone(tz):%b %d, %H:%M}"
    hold = routine.posture == "hold"
    goal = routine.instruction
    inputs: list[RawInput] = []
    if fire.payload is not None:
        goal += PAYLOAD_LAW.format(name=routine.name, source=fire.source.value)
        inputs = [RawInput(filename="trigger.json", data=fire.payload, media_type="application/json")]
    routine_id = routine.id

    try:
        if len(goal) > GOAL_CAP:
            # Belt under the column cap (the API door keeps INSTRUCTION_MAX headroom, but the
            # law must hold for any writer): a journaled refusal, never a DataError no net names.
            raise ValidationFailed(
                f"instruction + the payload fence exceed the {GOAL_CAP}-char goal cap - shorten the instruction"
            )
        task = await tasks_ops.create_task(
            session,
            owner,
            goal=goal,
            project_id=routine.project_id,
            clarify="never",  # nobody is there to answer an intake round - the standing rule
            budget={"usd": float(routine.budget_usd_per_run)},
            inputs=inputs,
            skills=list(template.skills),
            connectors=list(template.connectors),
            bots=[template.bot] if template.bot else [],
            direct=bool(template.bot),
            deliverable=template.deliverable,
            deliverable_format=template.deliverable_format,
            quality=template.quality,
            scale=template.scale or "auto",
            client_token=mint_client_token(routine_id, fire.occurrence),
            title=title[:200],
            hold=hold,
            origin_routine_id=routine_id,
            origin_depth=fire.chain_depth,
        )
    except PrismoidError as exc:
        # The mint refused (dead project role, dead pin, budget shape…). The refusal is a
        # journaled RESULT - and it COUNTS: consecutive mint refusals walk the same
        # auto-pause ladder as settled failures (Make counts errors; so do we).
        await session.rollback()
        relocked = await Routine.lock(session, routine_id)
        if relocked is None:
            return FireResult(run_id=None, decision=RunDecision.ERROR, detail=str(exc))
        advance_past(relocked, spec, fire.fire_instant, fire.now)
        await fire_failed(session, relocked, why=str(exc))
        logfire.warn("routine {rid} fire refused at mint: {err}", rid=str(routine_id), err=str(exc))
        fire.routine = relocked
        return await _refuse(
            session, fire, Refusal(RunDecision.ERROR, str(exc)[:400]), scheduled_for=fire.scheduled_for
        )

    relocked = await Routine.lock(session, routine_id)
    if relocked is None:
        return FireResult(
            run_id=None,
            decision=RunDecision.HELD if hold else RunDecision.MINTED,
            task_id=task.id,
            detail=fire.detail,
        )
    return await _mint_receipt(
        session,
        relocked,
        occurrence=fire.occurrence,
        source=fire.source,
        task=task,
        now=fire.now,
        scheduled_for=fire.scheduled_for,
        detail=fire.detail,
        chain_depth=fire.chain_depth,
    )


async def _mint_receipt(
    session: AsyncSession,
    routine: Routine,
    *,
    occurrence: str,
    source: RunSource,
    task: Task,
    now: dt.datetime,
    scheduled_for: dt.datetime | None,
    detail: str,
    chain_depth: int,
) -> FireResult:
    """The mint's receipt transaction, shared by the happy path and crash-resume recovery:
    advance (CAS - a parked fire's clock already moved, so it no-ops), clear the buffer slot,
    bump the counters, knock for a HELD task, journal. Idempotent under a racing twin: the
    unique occurrence hands back the original receipt."""
    spec = routine_gate.schedule_of(routine.trigger_kind, routine.trigger)
    fire_instant = routine_gate.fire_instant_of(occurrence)
    advance_past(routine, spec, fire_instant, now)
    if source == RunSource.CLOCK and spec is not None and spec.is_one_shot:
        routine.status = RoutineStatus.RETIRED.value  # fired once; the UI says "Ran"
    if routine.buffered_occurrence == occurrence:
        routine.buffered_occurrence = None  # the receipt clears the slot durably
    routine.runs_minted += 1
    routine.last_fired_at = now
    held = task.held_at is not None
    if held:
        await inbox_ops.routine_held(session, task, routine_id=routine.id, routine_name=routine.name)
    return await journal(
        session,
        routine,
        occurrence=occurrence,
        source=source,
        decision=RunDecision.HELD if held else RunDecision.MINTED,
        task_id=task.id,
        scheduled_for=scheduled_for,
        detail=detail,
        chain_depth=chain_depth,
    )


async def _retire_at_gate(
    session: AsyncSession,
    routine: Routine,
    occurrence: str,
    source: RunSource,
    why: str,
    *,
    scheduled_for: dt.datetime | None = None,
    chain_depth: int = 0,
) -> FireResult:
    routine.status = RoutineStatus.RETIRED.value
    routine.next_fire_at = None
    routine.buffered_occurrence = None
    await inbox_ops.routine_retired(
        session,
        actor_id=routine.owner_actor_id,
        routine_id=routine.id,
        routine_name=routine.name,
        why=f"Retired - {why}. Its run history stays readable.",
    )
    return await journal(
        session,
        routine,
        occurrence=occurrence,
        source=source,
        decision=RunDecision.SKIPPED_SEASON,
        scheduled_for=scheduled_for,
        detail=f"retired - {why}",
        chain_depth=chain_depth,
    )
