# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Routines - the verbs (ADR-048). A Routine executes nothing: `execute_fire` is the ONE policy
gate every source converges on (clock sweep, manual Run-now; the API door and the event
matcher join it), and the only thing a fire ever does is call `create_task`.

The fire is crash-safe by three idempotent layers, not by locking: the run ledger's unique
`(routine_id, occurrence)` - the receipt, re-checked UNDER the row lock; the `create_task`
client_token (a resumed or racing fire finds its already-minted task and writes the receipt it
was owed); and the tick-scoped DBOS workflow id (`routine-{id}-{occurrence}~{tick}` - replayed
enqueues within a tick dedupe, a crashed fire resumes, and a terminally-errored fire is simply
re-offered next tick instead of wedging its occurrence).
The sweep never advances state; `next_fire_at` moves only here, by
compare-and-advance against the occurrence that fired - which is also where the edit race
closes, and token/API fires never move the clock at all. Every fire and every NON-fire writes a
`routine_runs` row wearing its reason - silent suppression is the named industry defect;
the one deliberate deferral (the kill switch) is documented at the gate.
"""

from __future__ import annotations

import datetime as dt
import hmac
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.conf import settings
from prismoid.db.base import utcnow
from prismoid.db.models import tenant
from prismoid.db.models.actor import Actor
from prismoid.db.models.project import Project, Role
from prismoid.db.models.routine import Routine, RoutineRun
from prismoid.db.models.task import Task
from prismoid.db.models.user import User
from prismoid.db.session import row_scope
from prismoid.errors import (
    Conflict,
    FeatureDisabled,
    Forbidden,
    InvalidTransition,
    Unauthorized,
    ValidationFailed,
)
from prismoid.operations import inbox as inbox_ops
from prismoid.operations import usage as usage_ops
from prismoid.operations.routine_events import jitter, spec_of
from prismoid.operations.routine_events import match_window as match_window
from prismoid.operations.routine_events import on_settled as on_settled
from prismoid.operations.routine_fire import execute_fire as execute_fire
from prismoid.operations.throttle import FixedWindowLimiter
from prismoid.schemas.ids import hash_token, new_fire_token
from prismoid.schemas.routines import (
    CatchUp,
    EventSpec,
    FireResult,
    Notify,
    Overlap,
    PausedReason,
    Posture,
    RoutineStatus,
    RoutineTemplate,
    RunSource,
    ScheduleSpec,
    TriggerKind,
    month_start,
    next_fire_at,
    occurrence_for_api,
    occurrence_for_manual,
    parse_duration_s,
)


def _validated_spec(schedule: dict[str, Any]) -> ScheduleSpec:
    """Schemas raise plain ValueError with the user-facing sentence (the egress-door
    convention); the operation turns it into the 422."""
    try:
        return ScheduleSpec.model_validate(schedule)
    except ValueError as exc:
        raise ValidationFailed(_first_error_line(exc), fields={"schedule": _first_error_line(exc)}) from exc


def _first_error_line(exc: Exception) -> str:
    # pydantic wraps the validator's ValueError; the sentence we wrote is the one to surface.
    text = str(exc)
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("Value error, "):
            return line.removeprefix("Value error, ").split(" [type=")[0]
    return text.splitlines()[0] if text else "invalid schedule"


async def _fill_timezone(
    session: AsyncSession, actor: Actor, schedule: dict[str, Any], *, default: str | None = None
) -> dict[str, Any]:
    """`timezone: None` on the wire means "mine": the owner's profile timezone, else UTC - the
    wall clock a person writes is the one they mean.
    `default` is the EDIT semantics: a schedule PATCH that omits the timezone keeps the
    routine's existing one - silently re-resolving to the profile shifted a Berlin briefing to
    UTC on an unrelated cron edit."""
    if schedule.get("timezone"):
        return schedule
    if default:
        return {**schedule, "timezone": default}
    user = await session.get(User, actor.id)
    tz = getattr(user, "timezone", None) or "UTC"
    return {**schedule, "timezone": tz}


def _refuse_under_floor(spec: ScheduleSpec) -> None:
    """An interval under the floor is refused where it is computable - at the door, with the fix
    in the message (a cron under the floor is throttled honestly at fire time instead)."""
    if spec.every_s is not None and spec.every_s < settings.routines.min_interval_s:
        raise ValidationFailed(
            f"the floor between fires is {settings.routines.min_interval_s}s",
            fields={"every": f"use at least {settings.routines.min_interval_s}s, e.g. --every 5m"},
        )


def _refuse_naive_until(until_at: dt.datetime) -> None:
    if until_at.tzinfo is None:
        raise ValidationFailed(
            "an until-date must carry its timezone",
            fields={"until_at": "send an ISO-8601 datetime with an offset"},
        )


def _first_fire(spec: ScheduleSpec, routine_id: uuid.UUID) -> dt.datetime:
    """The schedule's first fire from now; a one-shot instant already past is a 422."""
    first = next_fire_at(spec, after=utcnow(), jitter_s=jitter(routine_id))
    if first is None:
        raise ValidationFailed(
            "that one-shot instant is already in the past",
            fields={"at": "pick a future time, or just run the task now"},
        )
    return first


# ── the standing object's verbs ───────────────────────────────────────────────────────────────


async def create_routine(
    session: AsyncSession,
    actor: Actor,
    *,
    name: str,
    instruction: str,
    schedule: dict[str, Any] | None = None,
    on_events: list[str] | None = None,
    event_verdict: str | None = None,
    project_id: uuid.UUID | None = None,
    budget_usd: float | None = None,
    bot: str | None = None,
    skills: list[str] | None = None,
    connectors: list[str] | None = None,
    deliverable: str | None = None,
    deliverable_format: str | None = None,
    quality: str | None = None,
    scale: str | None = None,
    overlap: str | None = None,
    catch_up: str | None = None,
    notify: str | None = None,
    posture: str | None = None,
    monthly_cap_usd: float | None = None,
    until_at: dt.datetime | None = None,
    max_runs: int | None = None,
    born_paused: bool = False,
) -> Routine:
    """One transaction; enqueue-free (the sweep finds `next_fire_at` on its own - a routine is
    a standing row, not a job)."""
    if not actor.is_human():
        raise Forbidden("only a human can own a routine")
    if project_id is not None:
        await Project.get_for(session, project_id, actor, min_role=Role.EDITOR)

    active = await Routine.active_count_for(session, actor.id)
    if active >= settings.routines.max_active_per_user:
        raise Conflict(
            f"you already have {active} routines - retire one first "
            f"(the cap is {settings.routines.max_active_per_user})"
        )

    if (schedule is None) == (on_events is None):
        raise ValidationFailed(
            "a routine has exactly one When: a schedule, or the events it listens to",
            fields={"schedule": "set schedule XOR on_events"},
        )

    if on_events is not None:
        # The event lane. Scope is the routine's own scoping, reused; posture
        # defaults to HOLD - an ambient trigger confirms before acting until the owner flips it
        # (Apple's eligibility taxonomy; the owner overrides by passing posture explicitly).
        try:
            event_spec = EventSpec(kinds=tuple(on_events), verdict=event_verdict)
        except ValueError as exc:
            raise ValidationFailed(
                _first_error_line(exc), fields={"on_events": _first_error_line(exc)}
            ) from exc
        return await _create_row(
            session,
            actor,
            routine_id=uuid.uuid4(),
            name=name,
            instruction=instruction,
            trigger_kind=TriggerKind.EVENT,
            trigger=event_spec.model_dump(mode="json"),
            next_fire=None,
            project_id=project_id,
            budget_usd=budget_usd,
            bot=bot,
            skills=skills,
            connectors=connectors,
            deliverable=deliverable,
            deliverable_format=deliverable_format,
            quality=quality,
            scale=scale,
            overlap=overlap,
            catch_up=catch_up,
            notify=notify,
            posture=posture if posture is not None else Posture.HOLD.value,
            monthly_cap_usd=monthly_cap_usd,
            until_at=until_at,
            max_runs=max_runs,
            born_paused=born_paused,
        )

    assert schedule is not None
    spec = _validated_spec(await _fill_timezone(session, actor, schedule))
    _refuse_under_floor(spec)
    if until_at is not None:
        _refuse_naive_until(until_at)

    routine_id = uuid.uuid4()  # minted here so the jitter offset exists before the row does
    first = _first_fire(spec, routine_id)

    return await _create_row(
        session,
        actor,
        routine_id=routine_id,
        name=name,
        instruction=instruction,
        trigger_kind=TriggerKind.SCHEDULE,
        trigger=spec.model_dump(mode="json"),
        next_fire=first,
        project_id=project_id,
        budget_usd=budget_usd,
        bot=bot,
        skills=skills,
        connectors=connectors,
        deliverable=deliverable,
        deliverable_format=deliverable_format,
        quality=quality,
        scale=scale,
        overlap=overlap,
        catch_up=catch_up,
        notify=notify,
        posture=posture,
        monthly_cap_usd=monthly_cap_usd,
        until_at=until_at,
        max_runs=max_runs,
        born_paused=born_paused,
    )


async def _create_row(
    session: AsyncSession,
    actor: Actor,
    *,
    routine_id: uuid.UUID,
    name: str,
    instruction: str,
    trigger_kind: TriggerKind,
    trigger: dict[str, Any],
    next_fire: dt.datetime | None,
    project_id: uuid.UUID | None,
    budget_usd: float | None,
    bot: str | None,
    skills: list[str] | None,
    connectors: list[str] | None,
    deliverable: str | None,
    deliverable_format: str | None,
    quality: str | None,
    scale: str | None,
    overlap: str | None,
    catch_up: str | None,
    notify: str | None,
    posture: str | None,
    monthly_cap_usd: float | None,
    until_at: dt.datetime | None,
    max_runs: int | None,
    born_paused: bool = False,
) -> Routine:
    template = RoutineTemplate(
        bot=bot,
        skills=tuple(skills or ()),
        connectors=tuple(connectors or ()),
        deliverable=deliverable,
        deliverable_format=deliverable_format,
        quality=quality,
        scale=scale,
    )
    routine = Routine(
        id=routine_id,
        name=name,
        instruction=instruction,
        owner_actor_id=actor.id,
        project_id=project_id,
        trigger_kind=trigger_kind,
        trigger=trigger,
        next_fire_at=next_fire,
        template=template.dump(),
        budget_usd_per_run=Decimal(
            str(budget_usd if budget_usd is not None else settings.routines.default_budget_usd)
        ),
        overlap=Overlap(overlap).value if overlap else Overlap.SKIP.value,
        catch_up=CatchUp(catch_up).value if catch_up else CatchUp.SKIP.value,
        notify=Notify(notify).value if notify else Notify.FINDINGS.value,
        posture=Posture(posture).value if posture else Posture.AUTO.value,
        monthly_cap_usd=Decimal(str(monthly_cap_usd)) if monthly_cap_usd is not None else None,
        until_at=until_at,
        max_runs=max_runs,
        min_interval_s=settings.routines.min_interval_s,
        daily_fire_cap=settings.routines.daily_fire_cap,
        pause_after_failures=settings.routines.pause_after_failures,
    )
    if born_paused:
        # An agent's proposal: the owner's RESUME is the arming ceremony - it also
        # recomputes next_fire_at from now, so a stale proposal never fires into the past.
        routine.status = RoutineStatus.PAUSED.value
        routine.paused_reason = PausedReason.USER.value
        routine.pause_note = "proposed by an agent - resume to arm"
        routine.next_fire_at = None
    session.add(routine)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise Conflict(f"a routine named {name!r} already exists in that scope") from exc
    return routine


async def update_routine(
    session: AsyncSession,
    actor: Actor,
    routine_id: uuid.UUID,
    *,
    name: str | None = None,
    instruction: str | None = None,
    schedule: dict[str, Any] | None = None,
    budget_usd: float | None = None,
    overlap: str | None = None,
    catch_up: str | None = None,
    notify: str | None = None,
    posture: str | None = None,
    monthly_cap_usd: float | None = None,
    until_at: dt.datetime | None = None,
    max_runs: int | None = None,
) -> Routine:
    """Edits apply from the next fire. A schedule change recomputes `next_fire_at` in THIS
    transaction - edit latency ≈ 0, and a fire already enqueued for the old instant will find
    the row changed and journal itself `superseded` (the compare-and-advance). PATCH semantics:
    None means leave alone; clearing a cap or a season is deferred with its reason (a PATCH
    cannot say both null and untouched in one field - the FaceIn clear-flag grammar, when a
    real user asks)."""
    routine = await Routine.get_for(session, routine_id, actor, min_role=Role.EDITOR)
    if routine.status == RoutineStatus.RETIRED:
        raise InvalidTransition("a retired routine is history - create a new one")
    if any(v is not None for v in (budget_usd, monthly_cap_usd, instruction, posture)):
        # OWNER verbs even in a shared project (like retire and the fire token): a fire runs as
        # the owner, on their money and connectors - an editor's instruction would be the owner's own turn,
        # a HOLD turned AUTO would skip their release, a money dial would raise their unattended spend.
        routine.require_owner(actor)
    if name is not None:
        routine.name = name
    if instruction is not None:
        routine.instruction = instruction
    _apply_dials(
        routine,
        budget_usd=budget_usd,
        overlap=overlap,
        catch_up=catch_up,
        notify=notify,
        posture=posture,
        monthly_cap_usd=monthly_cap_usd,
    )
    if until_at is not None:
        _refuse_naive_until(until_at)
        routine.until_at = until_at
    if max_runs is not None:
        routine.max_runs = max_runs
    if schedule is not None:
        await _reschedule(session, actor, routine, schedule)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise Conflict(f"a routine named {name!r} already exists in that scope") from exc
    return routine


def _apply_dials(
    routine: Routine,
    *,
    budget_usd: float | None,
    overlap: str | None,
    catch_up: str | None,
    notify: str | None,
    posture: str | None,
    monthly_cap_usd: float | None,
) -> None:
    """The PATCH's policy dials onto the row (None leaves a dial alone); writes no row itself."""
    if budget_usd is not None:
        routine.budget_usd_per_run = Decimal(str(budget_usd))
    if overlap is not None:
        routine.overlap = Overlap(overlap).value
        if routine.overlap != Overlap.QUEUE.value:
            routine.buffered_occurrence = None  # a park must not outlive queue mode
    if catch_up is not None:
        routine.catch_up = CatchUp(catch_up).value
    if notify is not None:
        routine.notify = Notify(notify).value
    if posture is not None:
        routine.posture = Posture(posture).value
    if monthly_cap_usd is not None:
        routine.monthly_cap_usd = Decimal(str(monthly_cap_usd))


async def _reschedule(
    session: AsyncSession, actor: Actor, routine: Routine, schedule: dict[str, Any]
) -> None:
    """A schedule PATCH: validate it and recompute `next_fire_at` from now, in the caller's
    transaction (commits nothing; the caller holds no row lock)."""
    if routine.trigger_kind != TriggerKind.SCHEDULE:
        # A ScheduleSpec written over an EventSpec (kind unflipped) made a routine the
        # sweep filters out AND the matcher can't match - permanently silent behind a 200.
        # Refuse loudly; converting kinds is a different feature.
        raise ValidationFailed(
            "this routine fires on events - its When is the event list, not a schedule",
            fields={"schedule": "create a schedule routine instead, or edit this one's events"},
        )
    if not any(schedule.get(k) is not None for k in ("cron", "every_s", "at")):
        # A timezone-only PATCH keeps the lane and moves the wall clock (the CLI's
        # `set --timezone` - previously a silent no-op).
        schedule = {**routine.trigger, "timezone": schedule.get("timezone")}
    spec = _validated_spec(
        await _fill_timezone(session, actor, schedule, default=routine.trigger.get("timezone"))
    )
    _refuse_under_floor(spec)
    first = _first_fire(spec, routine.id)
    routine.trigger = spec.model_dump(mode="json")
    routine.next_fire_at = first if routine.status == RoutineStatus.ACTIVE else None
    routine.buffered_occurrence = None  # a park predating the edit must not haunt the new schedule


async def pause_routine(
    session: AsyncSession, actor: Actor, routine_id: uuid.UUID, *, note: str | None = None
) -> Routine:
    routine = await Routine.get_for(session, routine_id, actor, min_role=Role.EDITOR)
    if routine.status == RoutineStatus.RETIRED:
        raise InvalidTransition("a retired routine cannot be paused")
    routine.status = RoutineStatus.PAUSED.value
    routine.paused_reason = PausedReason.USER.value
    routine.pause_note = note
    await session.commit()
    return routine


async def resume_routine(session: AsyncSession, actor: Actor, routine_id: uuid.UUID) -> Routine:
    """Resume RECOMPUTES the next fire from now - never from the stale past. This is also why a
    fire enqueued before a pause can never replay after it: its occurrence no longer matches
    the row, and the gate journals it superseded."""
    routine = await Routine.get_for(session, routine_id, actor, min_role=Role.EDITOR)
    if routine.status == RoutineStatus.RETIRED:
        raise InvalidTransition("a retired routine cannot be resumed")
    routine.status = RoutineStatus.ACTIVE.value
    routine.paused_reason = None
    routine.pause_note = None
    routine.consecutive_failures = 0  # resume is a fresh chance - the ladder starts over
    routine.buffered_occurrence = None  # a park predating the pause must not haunt the resume
    if routine.trigger_kind == TriggerKind.SCHEDULE:
        nxt = next_fire_at(spec_of(routine), after=utcnow(), jitter_s=jitter(routine.id))
        routine.next_fire_at = nxt
        if nxt is None:  # a one-shot resumed past its instant has nothing left to do
            routine.status = RoutineStatus.RETIRED.value
    await session.commit()
    return routine


async def retire_routine(session: AsyncSession, actor: Actor, routine_id: uuid.UUID) -> Routine:
    """Owner-only even in a shared project. Terminal; rows are never deleted."""
    routine = await Routine.get_for(session, routine_id, actor, min_role=Role.VIEWER)
    routine.require_owner(actor)
    routine.status = RoutineStatus.RETIRED.value
    routine.next_fire_at = None
    await session.commit()
    return routine


async def list_routines(
    session: AsyncSession,
    actor: Actor,
    *,
    project_id: uuid.UUID | None,
    limit: int,
    cursor: str | None,
) -> list[Routine]:
    return await Routine.list_for(session, actor, project_id=project_id, limit=limit, cursor=cursor)


async def get_routine(session: AsyncSession, actor: Actor, routine_id: uuid.UUID) -> Routine:
    return await Routine.get_for(session, routine_id, actor, min_role=Role.VIEWER)


def proposer(task_id: uuid.UUID, owner_actor_id: uuid.UUID, project_id: uuid.UUID | None):
    """The closure `routine.create` runs on: its OWN session (the step's sits
    committed-and-idle across the run - the emitter/tap law), the task's human owner as the
    routine's owner, BORN PAUSED, and the knock that says so. Returns the sentence the agent
    relays; raises PrismoidError sentences the tool converts to an in-band refusal."""

    async def propose(payload: dict[str, Any]) -> str:
        chosen = [k for k in ("cron", "every", "on_events") if payload.get(k)]
        if len(chosen) != 1:
            raise ValidationFailed("give exactly one of cron, every, or on_events")
        schedule = None
        if payload.get("cron") or payload.get("every"):
            schedule = {
                "cron": payload.get("cron"),
                "every_s": parse_duration_s(payload["every"]) if payload.get("every") else None,
                "at": None,
                "timezone": None,
            }
        async with row_scope(Task, task_id) as session:  # the routine is born in the task's workspace
            owner = await session.get(Actor, owner_actor_id)
            if owner is None:
                raise ValidationFailed("the task's owner is gone")
            routine = await create_routine(
                session,
                owner,
                name=str(payload["name"]),
                instruction=str(payload["instruction"]),
                schedule=schedule,
                on_events=list(payload["on_events"]) if payload.get("on_events") else None,
                project_id=project_id,
                budget_usd=payload.get("budget_usd"),
                born_paused=True,
            )
            routine.pause_note = f"proposed by an agent in task {task_id} - resume to arm"
            await inbox_ops.routine_paused(
                session,
                actor_id=owner_actor_id,
                routine_id=routine.id,
                routine_name=routine.name,
                why=(
                    "proposed by an agent from task work - review it on /routines and RESUME it "
                    "to arm; it never fires until you do"
                ),
                title=f"an agent proposed the routine {routine.name}",
            )
            await session.commit()
            return (
                f'Saved the routine "{routine.name}" - PAUSED, in the owner\'s name. It never '
                "fires until they review and resume it (they have an inbox note with the switch)."
            )

    return propose


async def spend_month_to_date(session: AsyncSession, routine: Routine) -> float:
    """The detail page's honest number: the ledger join, windowed by the routine's own
    calendar month (its schedule timezone, else UTC - the same window the cap reads)."""
    tz = routine.trigger.get("timezone", "UTC") if routine.trigger_kind == TriggerKind.SCHEDULE else "UTC"
    return float(await usage_ops.routine_month_to_date(session, routine.id, since=month_start(tz, utcnow())))


async def runs_for(
    session: AsyncSession,
    actor: Actor,
    routine_id: uuid.UUID,
    *,
    limit: int,
    cursor: str | None,
) -> list[tuple[RoutineRun, str | None]]:
    await Routine.get_for(session, routine_id, actor, min_role=Role.VIEWER)
    return await RoutineRun.page_with_status(session, routine_id, limit=limit, cursor=cursor)


# ── the fire gate ─────────────────────────────────────────────────────────────────────────────


async def fire(
    session: AsyncSession,
    actor: Actor,
    routine_id: uuid.UUID,
    *,
    payload: bytes | None = None,
    idempotency_key: str | None = None,
) -> FireResult:
    """The authed fire - present on every routine regardless of kind (the play-button rule).
    Editor+ (they can already create tasks in the scope; a routine is a saved task). A BARE
    fire is the human's play button (source=manual, policy-exempt); a fire carrying a payload
    or an Idempotency-Key is a MACHINE calling with credentials - it rides the api source and
    pays the full policy stack, exactly as a token fire would."""
    if not settings.routines.enabled:
        raise FeatureDisabled("routines are disabled on this instance")
    routine = await Routine.get_for(session, routine_id, actor, min_role=Role.EDITOR)
    if routine.status == RoutineStatus.RETIRED:
        raise Conflict("this routine is retired - create a new one to run it again")
    if payload is not None or idempotency_key is not None:
        return await execute_fire(
            session,
            routine_id,
            occurrence=occurrence_for_api(idempotency_key),
            source=RunSource.API,
            payload=payload,
        )
    return await execute_fire(
        session, routine_id, occurrence=occurrence_for_manual(), source=RunSource.MANUAL
    )


# ── the inbound door ─────────────────────────────────────────────────────────────────────

# The burst floor at the door: a leaked-or-looping caller must not buy a journal row per
# attempt. It counts AUTHENTICATED fires only - counting before the token check let junk-token
# spray 429-lock the legitimate webhook out of its own door, and the pre-auth, never-evicted
# dict was the attacker-fed memory leak the shared limiter exists to prevent.
# The per-routine min_interval floor inside the gate remains the durable throttle.
_FIRE_BURST_PER_MINUTE = 10
_fire_limiter = FixedWindowLimiter("fire calls")


async def mint_fire_token(session: AsyncSession, actor: Actor, routine_id: uuid.UUID) -> str:
    """Owner-only, shown ONCE, rotation supersedes (the ck_ ceremony). The token authenticates
    the ROUTINE, not a person: it can start runs and nothing else."""
    routine = await Routine.get_for(session, routine_id, actor, min_role=Role.VIEWER)
    routine.require_owner(actor)
    if routine.status == RoutineStatus.RETIRED:
        raise Conflict("a retired routine has no door to open")
    raw = new_fire_token()
    routine.fire_token_hash = hash_token(raw)
    await session.commit()
    return raw


async def revoke_fire_token(session: AsyncSession, actor: Actor, routine_id: uuid.UUID) -> None:
    routine = await Routine.get_for(session, routine_id, actor, min_role=Role.VIEWER)
    routine.require_owner(actor)
    routine.fire_token_hash = None
    await session.commit()


async def fire_via_token(
    session: AsyncSession,
    routine_id: uuid.UUID,
    raw_token: str,
    *,
    payload: bytes | None = None,
    idempotency_key: str | None = None,
) -> FireResult:
    """The token door: the route-level auth exception (the cn_/cr_ family - CLAUDE.md's list).
    Wrong, revoked, and never-minted are ONE indistinguishable 401; a paused routine refuses
    honestly (pausing stops machines too - only the human's play button outranks a pause);
    the payload rides the untrusted lane into /task/inputs/trigger.json."""
    if not settings.routines.enabled:
        raise FeatureDisabled("routines are disabled on this instance")
    routine = await session.get(Routine, routine_id)
    if routine is None or routine.fire_token_hash is None:
        raise Unauthorized("that fire token does not open this routine")
    if not hmac.compare_digest(routine.fire_token_hash, hash_token(raw_token)):
        raise Unauthorized("that fire token does not open this routine")
    # The token IS the routine: it acts for the ROUTINE's workspace, whatever workspace its owner's browser is
    # switched to - the minted task lands where the routine lives.
    tenant.bind(session, routine.workspace_id)
    _fire_limiter.hit(  # after the token check - only the real caller spends the window
        routine_id,
        per_minute=_FIRE_BURST_PER_MINUTE,
        message=f"this routine's fire door takes {_FIRE_BURST_PER_MINUTE} calls per minute",
    )
    if routine.status != RoutineStatus.ACTIVE:
        raise Conflict(f"this routine is {routine.status} - its door opens again when it resumes")
    return await execute_fire(
        session,
        routine_id,
        occurrence=occurrence_for_api(idempotency_key),
        source=RunSource.API,
        payload=payload,
    )
