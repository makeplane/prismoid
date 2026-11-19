# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Gates the system answers - the clock, the kill switch, and a control lease's ending.

A gate a person never answered still has to close: the TTL sweep resolves it by its own policy, the
kill switch declines the computer's gates, and a control lease's ending accepts the CONTROL gate it
opened. Each closes the gate under `resolve`'s guard (the task's row lock, the gate re-read under
it, a gate already closed left alone - the system never overwrites a person) and writes the same
`gate.resolved` record and wake a human's answer would. `gates.py` re-exports what callers use.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import logfire
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.conf import settings
from prismoid.db.base import utcnow
from prismoid.db.models import tenant
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.gate import Gate
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope
from prismoid.operations import dispatch, gate_answers, machines
from prismoid.operations import inbox as inbox_ops
from prismoid.operations.machines.screen import OFF
from prismoid.schemas.events import GateResolved
from prismoid.schemas.ids import GateId, ProjectId, TaskId
from prismoid.schemas.plan import GateAction, GateKind, GateResolution, GateSpec, GateState
from prismoid.schemas.screen import ControlEnd

GATE_TOPIC = "gate"  # the DBOS topic a parked workflow is listening on
CONTROL_ENDED_BY = "system (control ended)"  # who resolves a CONTROL gate when its lease ends
COMPUTER_USE_OFF_BY = "system (computer use off)"  # who declines one when the kill switch ends it


def wake_key(gate: Gate, *, turn: int) -> str:
    """Which workflow parked on this gate, and therefore where the answer must be sent.

    An **intake or budget** gate parked `run_task` - whose id is PER TURN
    (`task-{id}` for turn 1, `task-{id}-t{n}` after), so the caller passes the task's current
    turn; a gate can only be open while the turn that opened it is live, which is what makes
    the current turn the right address. A **mid-run** gate parked the step's `run_step`
    (`step-{id}`). Sending to the wrong one drops the answer into a recv nobody is waiting
    on - a quiet failure, since nothing errors: a budget gate at turn 2
    woken via `task-{id}` would poke a workflow that already returned while the real park
    slept its full 24 h. The gate's `step_id` is the discriminator, a real column for exactly
    this reason.
    """
    if gate.step_id is not None:
        return dispatch.step_job_key(gate.step_id)
    return dispatch.task_turn_job_key(gate.task_id, turn)


async def expire_open(session: AsyncSession, task: Task) -> None:
    """Nobody answered - or nobody CAN answer any more (delivery closes the book).

    **An unanswered gate that stays open is worse than no gate**: it sits in `prismoid gates`
    forever and teaches the user that the queue is noise. And an expiry that leaves no
    `gate.resolved` frame is a question that haunts every REPLAY forever - the web kept
    rendering a dock over a delivered answer because the closure never reached the stream
    (recovery once spawned four gates over a delivered board). So the row
    flips to EXPIRED **and the record says so**, resolved as DECLINE - the same wire shape a
    human's Skip writes, which every surface already folds. No wakes: the legitimate flows
    that expire (intake timeout already applied its defaults; delivery has no live steps)
    have nobody parked to wake.
    """
    now = utcnow()
    # The task's row first, THEN its gates (the order `resolve` and the sweep take): the open
    # gates are read under the lock, so an answer that committed a moment ago is seen as the
    # answer it is, never stamped EXPIRED over from a stale read.
    await Task.hold(session, task.id)
    expiring = await Gate.open_for_task(session, task.id)
    for gate in expiring:
        gate.state = GateState.EXPIRED.value
        gate.resolved_at = now
        seq = await task.next_seq(session)
        await TaskEvent.append(
            session,
            GateResolved(
                seq=seq,
                task_id=TaskId(task.id),
                project_id=ProjectId(task.project_id) if task.project_id else None,
                at=now,
                gate_id=GateId(gate.id),
                action=GateAction.DECLINE,
                content=None,
            ),
        )
        logfire.info("gate {gate} expired unanswered", gate=str(gate.id))
    await inbox_ops.gates_closed(session, task, [gate.id for gate in expiring], at=now)


async def wait_for_the_driver(session: AsyncSession, gate: Gate) -> bool:
    """An expired CONTROL gate whose task's browser someone still drives is not "nobody came": a
    lease renews without limit, so a long sign-in outlives the TTL, and `resolve` refuses a Skip
    for exactly this reason. Its clock moves on by `control_wait_s` instead, under `resolve`'s
    guard (the task's row lock, the gate re-read); the lease's ending answers it. The Machine
    cannot sleep meanwhile: the sweep never sleeps a leased row. True when it waits."""
    if await machines.screen.control_state(gate.task_id) is None:
        return False
    task = await session.get(Task, gate.task_id)
    if task is None:  # pragma: no cover - FK guarantees it
        return False
    tenant.bind(session, task.workspace_id)
    await session.refresh(task, with_for_update={"key_share": True})
    await session.refresh(gate)
    if gate.is_open():
        gate.expires_at = utcnow() + dt.timedelta(seconds=settings.computer_use.control_wait_s)
    await session.commit()
    return True


async def close_as_system(
    session: AsyncSession, gate: Gate, action: GateAction, *, content: dict[str, Any], by: str
) -> tuple[str, dict[str, Any]] | None:
    """Close a gate as the system and COMMIT it: the `resolve` path without an actor. Returns the
    wake to send - `(workflow key, payload)`, the `resolve_open_as_cancel` shape - or None when
    the gate had already closed. The caller sends it (or, resolving its own park, does not).

    The caller read the gate unlocked, so a human's answer can commit between that read and this
    write. The same guard as `resolve`: take the task's row lock, re-read the gate under it, and
    leave a closed gate alone - the system never overwrites a person."""
    task = await session.get(Task, gate.task_id)
    if task is None:  # pragma: no cover - FK guarantees it
        return None
    tenant.bind(session, task.workspace_id)  # the system acts for each gate's task workspace
    await session.refresh(task, with_for_update={"key_share": True})  # `resolve`'s lock, its strength
    await session.refresh(gate)
    if not gate.is_open():
        await session.commit()  # nothing written; release the row lock before the next gate
        return None

    resolution = GateResolution(action=action, content=content, resolved_by=by, at=utcnow())
    gate.state = GateState.RESOLVED.value
    gate.resolution = resolution.model_dump(mode="json")
    gate.resolved_at = resolution.at
    await inbox_ops.gates_closed(session, task, [gate.id], at=resolution.at)

    seq = await task.next_seq(session)
    await TaskEvent.append(
        session,
        GateResolved(
            seq=seq,
            task_id=TaskId(task.id),
            project_id=ProjectId(task.project_id) if task.project_id else None,
            at=resolution.at,
            gate_id=GateId(gate.id),
            action=action,
            content=content,  # type: ignore[arg-type]
        ),
    )
    await session.commit()
    return wake_key(gate, turn=task.turn), {**resolution.model_dump(mode="json"), "gate_id": str(gate.id)}


async def resolve_as_system(
    session: AsyncSession, gate: Gate, action: GateAction, *, content: dict[str, Any], by: str
) -> bool:
    """Resolve a gate as the system and wake its workflow - the clock (`resolve_by_timeout`), the
    kill switch. False when the gate had already closed (`close_as_system`)."""
    woken = await close_as_system(session, gate, action, content=content, by=by)
    if woken is None:
        return False
    key, payload = woken
    await dispatch.get_dispatcher().send(key, payload, topic=GATE_TOPIC)
    return True


async def resolve_by_timeout(session: AsyncSession, gate: Gate, action: GateAction) -> bool:
    """Resolve an expired gate as the system, and wake its workflow - the `resolve` path without an
    actor, because the actor here is *the clock*. False when the gate had already closed. A
    declined question gate carries its defaults; a CONTROL gate says nobody came."""
    spec = GateSpec.model_validate(gate.spec)
    answers: dict[str, Any] = {}
    if spec.kind is GateKind.CONTROL:
        answers = {"outcome": "timeout"}
    elif action is GateAction.DECLINE and spec.questions:
        answers = gate_answers.decline_answers(spec)
    return await resolve_as_system(session, gate, action, content=answers, by="system (timeout)")


def answered_by(gate: Gate, *, lease_since: dt.datetime, ended_at: dt.datetime | None) -> bool:
    """Whether the ending of the lease claimed at `lease_since` answers this open CONTROL gate.

    A takeover gate opened on the lease claimed at `GateSpec.lease_since`: that lease's ending
    answers it - even one that landed just before the gate committed (the race: equal `since`) -
    and so does any LATER lease's (`since` after it): the holder's switch to Private is a fresh
    claim with a new `since` and no ending, and a later holder may drive after an
    ending that failed to answer. An EARLIER lease's late ending - a run-out lease the next claim
    ends first - never does.

    A handoff gate belongs to no lease: only an ending at or after it opened answers it
    (`ended_at`, the lease's end or its `until` when its clock ran out first), so a lease that ran
    out BEFORE the agent asked - ended later by the sweep or the next claim - never closes it."""
    if gate.kind != GateKind.CONTROL.value:
        return False
    spec = GateSpec.model_validate(gate.spec)
    if spec.source == "takeover" and spec.lease_since:
        return lease_since >= dt.datetime.fromisoformat(spec.lease_since)
    return ended_at is not None and gate.created_at <= ended_at


async def resolve_control(
    task_id: uuid.UUID,
    *,
    holder: str,
    held_s: int,
    reason: ControlEnd,
    lease_since: dt.datetime,
    ended_at: dt.datetime | None,
    wake: bool = True,
) -> bool:
    """A control lease ended (the one claimed at `lease_since`), so answer the task's open CONTROL
    gate it answers (`answered_by`) as the system: any ending ACCEPTS it with
    `{outcome: accepted, holder, held_s, reason}`, which becomes the parked call's text on resume -
    except the kill switch's (`disabled`), which DECLINES it with
    `{outcome: disabled, reason}`: nobody handed anything back, and the resumed step meets the
    switch before its Machine. Its own transaction, AFTER the lease's commit (lock order: this
    takes the task's row, never the Machine's).

    True when THIS call closed the gate - even if the wake after the commit then failed (logged:
    the gate is closed, and the parked recv's gate-row re-read is the net). False when no gate it
    answers is open (the common case: a person drove an idle Machine) or someone got there first.
    Never raises: the gate's TTL is the backstop. `wake=False` only for the parked step's own race
    settle (`execution.wake.settle_lease_race`), which resumes in place instead of being woken."""
    if reason is ControlEnd.DISABLED:
        action, by = GateAction.DECLINE, COMPUTER_USE_OFF_BY
        content: dict[str, Any] = {"outcome": "disabled", "reason": OFF}
    else:
        action, by = GateAction.ACCEPT, CONTROL_ENDED_BY
        content = {"outcome": "accepted", "holder": holder, "held_s": held_s, "reason": str(reason)}
    closed = False
    try:
        async with row_scope(Task, task_id) as session:
            if tenant.bound_workspace(session) is None:  # the task is gone
                return False
            gate = next(
                (
                    g
                    for g in await Gate.open_for_task(session, task_id)
                    if answered_by(g, lease_since=lease_since, ended_at=ended_at)
                ),
                None,
            )
            if gate is None:
                return False
            woken = await close_as_system(session, gate, action, content=content, by=by)
            if woken is None:
                return False
            closed = True
            if wake:
                key, payload = woken
                await dispatch.get_dispatcher().send(key, payload, topic=GATE_TOPIC)
            return True
    except Exception:
        logfire.exception(
            "task {task}: the CONTROL gate could not be answered (closed: {closed}); its TTL will",
            task=str(task_id),
            closed=closed,
        )
        return closed
