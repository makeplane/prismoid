# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Steering - the human's hands on a running task.

Four verbs, and the rule that separates them is **when they take effect**:

* **cancel** is the only hard stop. It stops the task now, wherever it is, because "stop" is the one
  instruction that cannot wait for a safe point - a user who wants to stop wants to stop. A tool
  already running finishes; the call in flight is not aborted (early cancel is not built).
* **steer** and **pause** apply at **safe points**: before each model request, before each tool
  dispatch, when a step's run is about to end (inside a running step, through the `Steering`
  capability in `ai/capabilities/steering.py`), and at every frontier boundary between waves. A steer is
  a durable command accepted under the task row lock by compare-and-set on `tasks.plan_epoch`
  (a stale one is a 409), and before a turn delivers every steer accepted in it is applied,
  rejected or retracted. A pause parks a step that can defer at its next tool dispatch; any other
  step pauses at the frontier.
* **follow-up** is not an interruption either - it is the SAME task continuing: a settled
  task takes the new message as its next turn (`operations/tasks.continue_task`), re-enters
  planning with the whole conversation in hand, and delivers again on the same record. Steering is
  the mid-flight channel; the follow-up is the settled-boundary one; fork is the branch.

Every steer and pause lands in `plan_edits`, which is append-only: *what the human asked
for, and when* is part of the task's history, not a mutation of its plan. The plan the model made and
the plan the human bent are both legible, forever.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

import logfire
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.tools import catalog
from prismoid.db.base import utcnow
from prismoid.db.models.actor import Actor
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.plan import PlanEdge, PlanEdit, PlanStep
from prismoid.db.models.steer import Steer
from prismoid.db.models.task import Task
from prismoid.errors import Conflict, Forbidden, NotFound, StaleSteer, ValidationFailed
from prismoid.operations import actor_names, allowance, call_log, context, device_grants, dispatch, stopped
from prismoid.operations.attribution import actor_ref
from prismoid.operations.stopped import reject_unapplied as reject_unapplied
from prismoid.schemas.events import (
    PauseApplied,
    PlanEdited,
    SteerApplied,
    SteerRetracted,
    TaskBudgetSet,
    TaskMoved,
    TaskPlanned,
    TaskRenamed,
    TaskStatusChanged,
)
from prismoid.schemas.ids import ArtifactId, ProjectId, StepId, TaskId
from prismoid.schemas.plan import PlanEdit as PlanEditView
from prismoid.schemas.plan import PlanEditKind, StepSpec, StepState, TaskStatus
from prismoid.schemas.side_effects import SideEffect
from prismoid.schemas.steering import (
    DIRECTIVE_MAX,
    REJECT_CANCELLED,
    MissedSteer,
    SafePoint,
    SteerNote,
    SteerState,
    missed_message,
    preview,
)

if TYPE_CHECKING:
    from prismoid.operations.turn.replan import Replanned

STEER_TOPIC = "steer"  # the DBOS topic a paused run_task listens on for its resume
_ACTS_IN_THE_WORLD = frozenset({"action", "navigate"})  # routes the fences never run again (R45)
# A step runs again only if nothing it called could have acted outside the task: no world act in the
# tool catalog, no connector tool (a read too: conservative), no untagged name, no shell with a route
# out (a failed call too). Notes, never calls: an egress refusal, a dropped connector (`connector.<slug>`).
_NOTES = ("frame.egress", "connector.")
_SHELL = "frame.shell"


async def steer(
    session: AsyncSession,
    actor: Actor,
    task_id: uuid.UUID,
    directive: str,
    *,
    command_id: uuid.UUID | None = None,
    issued_epoch: int | None = None,
) -> Steer:
    """Accept a steer: durable, attributed, idempotent on `command_id`, ordered by
    compare-and-set on the plan epoch under the task row lock. The running step takes it at its next
    safe point; this door never touches a running step - only an approval a parked step waits on,
    which the steer supersedes (`gates.supersede_approvals`).

    Every check that accepts it runs here, under the lock, so a refused steer (403, 409, 422) leaves
    no row: received and accepted land in one commit. `issued_epoch` is the epoch the author's screen
    showed when they began typing; a steer issued against an older one is a 409 naming what they
    missed - unless all that moved it was their own steers (R51). Without it (the chat path, older
    clients) a steer is never stale.
    """
    directive = directive.strip()
    if not directive:
        raise ValidationFailed("a steer needs words", fields={"directive": "empty"})
    if len(directive) > DIRECTIVE_MAX:  # the chat door's lines run longer than the API's cap
        raise ValidationFailed(
            f"a steer is at most {DIRECTIVE_MAX} characters", fields={"directive": "too long"}
        )
    from prismoid.operations import gates

    task = await Task.get_for(session, task_id, actor)  # scope first
    await require_project_editor(session, task, actor)  # editor+ on a project task
    checked = task.project_id
    # The house lock: the epoch moves only under it, so two steers (or a steer and a turn's start)
    # are ordered, and the epoch read below is the committed one.
    task = await Task.held(session, task.id)
    await _recheck_after_move(session, task, actor, checked)

    if command_id is not None and (known := await Steer.by_command(session, task.id, command_id)) is not None:
        return known  # a resend: the same command, the same answer
    if task.is_terminal():
        raise _finished(task, "steer")
    if issued_epoch is not None and not 0 <= issued_epoch <= task.plan_epoch:
        raise ValidationFailed("unknown epoch", fields={"issued_epoch": "unknown epoch"})
    if (
        issued_epoch is not None
        and issued_epoch < task.plan_epoch
        and not await _only_own_since(session, task, actor, issued_epoch)
    ):
        missed = await _missed(session, task, actor, issued_epoch)
        raise StaleSteer(missed_message(missed), [m.model_dump(mode="json") for m in missed])

    task.plan_epoch += 1
    task.epoch_actor_id = actor.id  # spend follows the epoch
    row = Steer(
        task_id=task.id,
        command_id=command_id or uuid.uuid4(),
        turn=task.turn,
        actor_id=actor.id,
        text=directive,
        issued_epoch=issued_epoch,
        epoch=task.plan_epoch,
    )
    session.add(row)
    # `turn` binds the directive to the turn it was given in: the boundary merge applies only the
    # current turn's steers, while the thread transcript still renders every one, honestly.
    session.add(
        PlanEdit(
            task_id=task.id,
            kind="steer",
            reason=directive,
            payload={
                "directive": directive,
                "turn": task.turn,
                "command_id": str(row.command_id),
                "epoch": row.epoch,
            },
            actor_id=actor.id,
        )
    )
    # **A held fork launches on its first directive** (user law v4): the hold clears in the
    # same transaction the directive lands in, and the enqueue follows the commit.
    if launch := task.held_at is not None and task.state is TaskStatus.CREATED:
        await allowance.require(session, task.workspace_id, actor=actor)  # launching is new spend
        task.held_at = None
    await session.flush()

    now = utcnow()
    seq = await task.next_seq(session)
    await TaskEvent.append(
        session,
        PlanEdited(
            seq=seq,
            task_id=TaskId(task.id),
            project_id=ProjectId(task.project_id) if task.project_id else None,
            at=now,
            actor=actor_ref(actor),
            edit=PlanEditView(
                kind=PlanEditKind.STEER,
                reason=directive,
                at=now,
                command_id=str(row.command_id),
                epoch=row.epoch,
            ),
        ),
    )
    # An open approval this steer overtakes is superseded in this transaction:
    # the write it would approve was chosen before the steer, so it never runs; its step takes
    # the steer here (`applied{approval}`) and is told, on its wake, that the call was held.
    superseded = await gates.supersede_approvals(session, task, actor, row)
    taken = await notes(session, [row]) if superseded else ()
    steps = {s.id: s for s in await PlanStep.for_task(session, task.id)} if superseded else {}
    for _key, _payload, gate_step_id in superseded:
        step = steps.get(gate_step_id) if gate_step_id else None
        await apply(session, task, step, taken, SafePoint.APPROVAL)
    await session.commit()
    for key, payload, _ in superseded:  # after commit: each parked step's `run_step`
        await dispatch.get_dispatcher().send(key, payload, topic=gates.GATE_TOPIC, missing_ok=True)
    if launch:
        from prismoid.operations.dispatch import get_dispatcher, task_turn_job_key

        await get_dispatcher().enqueue(
            "run_task", task_turn_job_key(task.id, task.turn), task_id=str(task.id), turn=task.turn
        )
        logfire.info("held fork {task} launched by its first directive", task=str(task.id))
    logfire.info("task {task} steered at epoch {epoch}", task=str(task.id), epoch=row.epoch)
    return row


async def _recheck_after_move(
    session: AsyncSession, task: Task, actor: Actor, checked: uuid.UUID | None
) -> None:
    """A `move` may commit between the editor check (an unlocked read) and the row lock:
    the task under the lock is somewhere else, so the check runs again there. Moved home, it
    is its creator's alone - anyone else gets the answer `Task.get_for` gives a stranger."""
    if task.project_id == checked:
        return
    if task.project_id is None and task.creator_actor_id != actor.id:
        raise NotFound("no such task")
    await require_project_editor(session, task, actor)


async def _only_own_since(session: AsyncSession, task: Task, actor: Actor, issued_epoch: int) -> bool:
    """A steer is not stale for its author's OWN later steers: only another person's
    steer, or a turn's start, is something they missed. True when no turn opened past
    `issued_epoch` and every steer since is theirs - the steer is then accepted at the current
    epoch."""
    if task.turn_epoch > issued_epoch:
        return False
    return all(row.actor_id == actor.id for row in await Steer.since(session, task.id, issued_epoch))


async def _missed(session: AsyncSession, task: Task, actor: Actor, issued_epoch: int) -> list[MissedSteer]:
    """What a stale steer's author had not seen, oldest first: other people's later steers, and a
    turn's start (their own included - a turn moves the task on whoever sent it). Their own steers
    are not news to them (R51), so the 409's first line names the teammate. One query names all."""
    others = [row for row in await Steer.since(session, task.id, issued_epoch) if row.actor_id != actor.id]
    turned = task.turn_epoch > issued_epoch
    turn_author = (task.turn_actor_id or task.creator_actor_id) if turned else None
    names = await actor_names.names_of(session, [*(r.actor_id for r in others), turn_author])
    out = [
        MissedSteer(
            kind="steer", author=names[r.actor_id], text=preview(r.text), epoch=r.epoch, state=r.state
        )
        for r in others
    ]
    if turned:
        out.append(MissedSteer(kind="turn", author=names[turn_author], epoch=task.turn_epoch))
    return sorted(out, key=lambda m: m.epoch)


async def retract(session: AsyncSession, actor: Actor, task_id: uuid.UUID, command_id: uuid.UUID) -> Steer:
    """The author withdraws an unapplied steer. The task row is taken first (the order
    every steer writer uses: task, then steer and step rows); a compare-and-set on the steer row
    decides a race with a safe point's apply. The epoch never moves back."""
    task = await Task.get_for(session, task_id, actor)
    await require_project_editor(session, task, actor)
    checked = task.project_id
    task = await Task.held(session, task.id)  # its Project as the lock sees it (R35)
    await _recheck_after_move(session, task, actor, checked)
    row = await Steer.by_command(session, task.id, command_id)
    if row is None:
        raise NotFound("no such steer")
    if row.actor_id != actor.id:
        raise Forbidden(f"only {await actor_names.name_of(session, row.actor_id)} can retract this steer")
    if not await Steer.settle(session, task.id, [command_id], to=SteerState.RETRACTED):
        await session.refresh(row)
        raise Conflict(f"this steer was already {row.state}")
    seq = await task.next_seq(session)
    await TaskEvent.append(
        session,
        SteerRetracted(
            seq=seq,
            task_id=TaskId(task.id),
            project_id=ProjectId(task.project_id) if task.project_id else None,
            at=utcnow(),
            actor=actor_ref(actor),
            command_id=command_id,
        ),
    )
    await session.commit()
    await session.refresh(row)
    logfire.info("task {task} steer at epoch {epoch} retracted", task=str(task.id), epoch=row.epoch)
    return row


async def notes(session: AsyncSession, rows: Sequence[Steer]) -> tuple[SteerNote, ...]:
    """Steer rows as the engine reads them: each author a safe display name, and whether they
    created the task (a step's connectors are the creator's), oldest first as given."""
    out: list[SteerNote] = []
    names = await actor_names.names_of(session, [row.actor_id for row in rows])
    for row in rows:
        task = await session.get(Task, row.task_id)  # the creator never changes: any copy answers
        out.append(
            SteerNote(
                command_id=row.command_id,
                epoch=row.epoch,
                author=names[row.actor_id],
                text=row.text,
                by_creator=task is not None and row.actor_id == task.creator_actor_id,
            )
        )
    return tuple(out)


async def apply(
    session: AsyncSession,
    task: Task,
    step: PlanStep | None,
    taken: Sequence[SteerNote],
    point: SafePoint,
    *,
    superseded: Sequence[uuid.UUID] = (),
) -> None:
    """Running work took these steers: the step's epoch and label move to the newest,
    each still-accepted steer becomes applied, and the first application of each is on the record.

    The task row first, as every steer writer. A point may report the same steers again (a held call,
    then its request): the compare-and-set settle keeps the first, but a re-run says what it superseded
    for each. The record names what the step already did outside the task. The caller commits."""
    await Task.hold(session, task.id)
    if not taken:
        return
    newest = max(taken, key=lambda n: n.epoch)
    if step is not None and newest.epoch > step.epoch:
        opener = await Steer.at_epoch(session, task.id, newest.epoch)
        step.epoch = newest.epoch
        step.epoch_actor_id = opener.actor_id if opener is not None else step.epoch_actor_id
    moved = await Steer.settle(
        session,
        task.id,
        [n.command_id for n in taken],
        to=SteerState.APPLIED,
        safe_point=point.value,
        step_id=step.id if step is not None else None,
    )
    first = {row.command_id for row in moved}
    already = await acted_already(session, task, step) if step is not None and (first or superseded) else []
    for note in sorted(taken, key=lambda n: n.epoch):
        if not superseded and note.command_id not in first:
            continue
        seq = await task.next_seq(session)
        await TaskEvent.append(
            session,
            SteerApplied(
                seq=seq,
                task_id=TaskId(task.id),
                step_id=StepId(step.id) if step is not None else None,
                project_id=ProjectId(task.project_id) if task.project_id else None,
                at=utcnow(),
                command_id=note.command_id,
                step_title=step.title if step is not None else "",
                author=note.author,
                safe_point=point,
                superseded=[ArtifactId(a) for a in superseded],
                already_done=already,
            ),
        )


async def apply_planned(session: AsyncSession, task: Task, drafted: int) -> None:
    """The plan took every steer of the turn up to the epoch planning read: its
    transcript carried them, so each still accepted - one sent while the task clarified or planned
    included - is `applied{replan}`. The caller commits."""
    rows = [r for r in await Steer.unapplied(session, task.id, turn=task.turn) if r.epoch <= drafted]
    if rows:
        await apply(session, task, None, await notes(session, rows), SafePoint.REPLAN)


async def boundary(session: AsyncSession, task_id: uuid.UUID) -> Replanned:
    """At a frontier boundary, re-plan unstarted steps drafted before the current epoch in one call;
    nothing unstarted, nothing to do. The caller commits, then says how."""
    from prismoid.operations.turn import replan  # a cycle: the re-plan applies its steers through here

    task = await session.get(Task, task_id)
    if task is None or task.is_terminal() or not (taken := await pending(session, task)):
        return replan.UNMOVED
    return await replan.at_boundary(session, task, taken)


async def pending(session: AsyncSession, task: Task) -> tuple[SteerNote, ...]:
    """The turn's live steers past the oldest unstarted step's epoch, up to the plan epoch as loaded."""
    steps = await PlanStep.for_task(session, task.id)
    epochs = [s.epoch for s in steps if s.step_state in (StepState.PENDING, StepState.READY)]
    if not epochs:
        return ()
    # Up to the epoch the re-plan will read (the row as loaded): a steer landing after it is the
    # next boundary's, so the reason names only what the new plan takes.
    since = [
        r
        for r in await Steer.newer_than(session, task.id, turn=task.turn, epoch=min(epochs))
        if r.epoch <= task.plan_epoch
    ]
    return await notes(session, since)


async def seen_by(session: AsyncSession, task: Task, step: PlanStep) -> list[Steer]:
    """What this step has taken, oldest first: its turn's live steers up to its epoch (the attempt's
    contract and the verify receipt both read it)."""
    return await Steer.seen_by(session, task.id, floor=step.turn_epoch, epoch=step.epoch)


async def unseen(session: AsyncSession, task: Task, step: PlanStep) -> list[Steer]:
    """The turn's live steers THIS step has not taken, oldest first: past its epoch,
    accepted or applied - a sibling's apply does not count as this step's."""
    return await Steer.newer_than(session, task.id, turn=task.turn, epoch=step.epoch)


async def turn_sinks(session: AsyncSession, task: Task) -> list[PlanStep]:
    """What the turn delivers (fence 2): done steps nothing depends on, minus a replaced sink and a step
    that handed the turn to a plan (keyed on `plan_edits{hatch|lead}`)."""
    hatched = {
        str(edit.payload.get("step_id"))
        for kind in (PlanEditKind.HATCH, PlanEditKind.LEAD)
        for edit in await PlanEdit.of_kind(session, task.id, kind.value)
    }
    feeding = {e.from_step_id for e in await PlanEdge.for_task(session, task.id)}
    return [
        s
        for s in await PlanStep.for_task(session, task.id)
        if s.turn_epoch == task.turn_epoch
        and s.step_state is StepState.DONE
        and s.id not in feeding
        and str(s.id) not in hatched
    ]


def acted_outside(tool: str, *, egress: bool) -> bool:
    """Did this journaled call act outside the task, or may it have? `egress`: its shell had a route out."""
    if tool == _SHELL:
        return egress
    world = catalog.side_effect_of(tool) in (None, SideEffect.WORLD) and not tool.startswith(_NOTES)
    return world or catalog.mints(tool)


async def acted_already(session: AsyncSession, task: Task, step: PlanStep) -> list[str]:
    """This attempt's calls that acted outside the task, or may have: what a steer can no longer change."""
    from prismoid.operations import execution

    tools = await TaskEvent.tools_called(session, task_id=task.id, step_id=step.id, attempt=step.attempt)
    spec = StepSpec.model_validate(step.spec)
    shell = _SHELL in tools and spec.contract is not None
    egress = shell and bool(await execution.frame_allowlist(session, task, spec))
    return sorted(tool for tool in tools if acted_outside(tool, egress=egress))


async def reruns(session: AsyncSession, task: Task, step: PlanStep) -> bool:
    """May a fence run this step again? Never on an action or navigate turn, nor after `acted_already`."""
    planned = await TaskEvent.latest_of_type(session, task.id, "task.planned")
    route = planned.route if isinstance(planned, TaskPlanned) else "standard"
    return route not in _ACTS_IN_THE_WORLD and not await acted_already(session, task, step)


async def texts_of_turn(session: AsyncSession, task: Task) -> list[str]:
    """The current turn's live steers, oldest first - part of memory_extract's brief."""
    rows = await Steer.seen_by(session, task.id, floor=task.turn_epoch, epoch=task.plan_epoch)
    return [r.text for r in rows]


async def pause_applied(
    session: AsyncSession, task: Task, step_id: uuid.UUID | None, point: SafePoint
) -> None:
    """The pause took hold: "Pausing… (Ada)" becomes "Paused by Ada". Once per
    pause: every safe point that meets it reports it (each run's inbox, the frontier), so a fact
    already written since the task's latest status change is the one that stands. Read under the
    task's row lock, so two reporting at once write one, and a resume that committed after the
    safe point read the pause leaves nothing to say. The caller commits."""
    task = await Task.held(session, task.id)
    if task.state is not TaskStatus.PAUSED:
        return
    moved = await TaskEvent.latest_of_type(session, task.id, "task.status")
    noted = await TaskEvent.latest_of_type(session, task.id, "pause.applied")
    if noted is not None and noted.seq > (moved.seq if moved is not None else 0):
        return
    seq = await task.next_seq(session)
    await TaskEvent.append(
        session,
        PauseApplied(
            seq=seq,
            task_id=TaskId(task.id),
            step_id=StepId(step_id) if step_id is not None else None,
            project_id=ProjectId(task.project_id) if task.project_id else None,
            at=utcnow(),
            safe_point=point,
        ),
    )


async def pause(session: AsyncSession, actor: Actor, task_id: uuid.UUID) -> str:
    """Ask the task to stop at its next safe point: a running step that can park parks at its next
    tool dispatch, any other finishes, and the frontier dispatches nothing new. `resume`
    picks up exactly there."""
    task = await Task.get_for(session, task_id, actor)
    await require_project_editor(session, task, actor)  # editor+ on a project task
    # The status as the lock sees it: a delivery or a cancel committing meanwhile is waited for and
    # read, never paused over through the copy read above.
    task = await Task.held(session, task.id)
    if task.state not in (TaskStatus.RUNNING, TaskStatus.WAITING):
        raise _finished(task, "pause") if task.is_terminal() else _wrong_state(task, "pause")

    # The status flip is what `run_task` reads at the boundary and a running step's safe points read
    # before each tool. Marked here, honoured there - a pause never cuts a tool already running.
    await _advance(session, task, TaskStatus.PAUSED, actor=actor)
    await session.commit()
    logfire.info("task {task} pausing at the next safe point", task=str(task.id))
    return task.status


async def resume(session: AsyncSession, actor: Actor, task_id: uuid.UUID) -> str:
    """Un-pause. The parked `run_task` is woken by a message and continues the frontier loop; a
    step parked mid-run on the pause has its PAUSE gate lifted and continues its run."""
    from prismoid.db.models.gate import Gate
    from prismoid.operations import gates
    from prismoid.schemas.plan import GateKind

    task = await Task.get_for(session, task_id, actor)
    await require_project_editor(session, task, actor)  # editor+ on a project task
    # The row lock first, then the status under it: a step parking on the pause takes the same
    # lock to read it, so its gate lands before this (and is lifted below) or sees `running`.
    task = await Task.held(session, task.id)
    if task.state is not TaskStatus.PAUSED:
        raise _wrong_state(task, "resume")

    wakes = await gates.resolve_pause(session, task, actor)
    await _advance(session, task, TaskStatus.RUNNING, actor=actor)
    if any(g.kind != GateKind.PAUSE.value for g in await Gate.open_for_task(session, task.id)):
        # A question, a connect block or an approval is still open: the task is
        # waiting on a person again, as it was before the pause - its answer moves it on.
        await _advance(session, task, TaskStatus.WAITING)
    await session.commit()

    # After commit: wake the parked workflow per turn (`task-{id}` after turn 1 is a finished
    # workflow, and a message there wakes nobody).
    d = dispatch.get_dispatcher()
    await d.send(dispatch.task_turn_job_key(task.id, task.turn), {"resume": True}, topic=STEER_TOPIC)
    for key, body in wakes:  # each parked step's `run_step`, on its own address
        await d.send(key, body, topic=gates.GATE_TOPIC, missing_ok=True)
    logfire.info("task {task} resumed", task=str(task.id))
    return task.status


async def cancel(session: AsyncSession, actor: Actor, task_id: uuid.UUID) -> str:
    """Stop now - the only mid-step interrupt. A running step's frame is reaped by the
    workflow's own failure path when its next durable write finds the task cancelled."""
    task = await Task.get_for(session, task_id, actor)
    await require_project_editor(session, task, actor)  # editor+ on a project task
    return await cancel_task_row(session, task, actor)


async def cancel_task_row(session: AsyncSession, task: Task, actor: Actor, *, attribute: bool = True) -> str:
    """The cancel machinery after fetch + authz - ONE implementation, two doors.

    The user door (`cancel`, above) reaches it through `Task.get_for` + the project-editor check;
    the operator door (`operations/admin_ops.cancel_admin`) reaches it through the audited admin
    fetch that deliberately skips visibility. The machinery must not fork: a cancel that wakes
    gates on one door and forgets them on the other is two products wearing one verb.

    `attribute=False` is for a SYSTEM cancel that borrows the owner's authority (a Routine's
    REPLACE overlap): the events it writes carry no actor (machine-written events
    stay actor-less), where a human's or an operator's cancel names them.
    """
    from prismoid.operations import gates

    task = await Task.held(session, task.id)  # a delivery committing meanwhile is waited for and read
    if task.is_terminal():
        return task.status  # already stopped; cancelling a finished task is a no-op, not an error

    await _advance(session, task, TaskStatus.CANCELLED, actor=actor if attribute else None)
    await call_log.forget(session, task.id)
    await reject_unapplied(session, task, REJECT_CANCELLED)  # what never applied says so

    # A held fork dies clean: the hold is user-space, and a cancelled task can never be steered
    # again to clear it - without this the branch says "holding" forever with no way out.
    if task.held_at is not None:
        task.held_at = None

    # Close every open gate as cancel - budget/clarify/ask_user parks listen on GATE_TOPIC
    # (and mid-run parks on `step-{id}`); only poking STEER left them asleep for 24 h.
    # `gates.resolve_open_as_cancel` is the one home for what resolving-as-cancel means.
    wakes = await gates.resolve_open_as_cancel(session, task, actor, attribute=attribute)

    await session.commit()

    # Pause parks on STEER; gate parks on GATE (task or step key). Always poke STEER so a pause-without-gate
    # wakes immediately; poke every open gate's exact workflow. Task-level addresses are per turn - the park
    # lives in the CURRENT turn's workflow. missing_ok: a HELD task (holds and forks alike) never enqueued its
    # workflow - the poke legitimately has no addressee; the cancel itself is committed.
    d = dispatch.get_dispatcher()
    await d.send(
        dispatch.task_turn_job_key(task.id, task.turn),
        {"cancelled": True},
        topic=STEER_TOPIC,
        missing_ok=True,
    )
    for key, body in wakes:
        await d.send(key, body, topic=gates.GATE_TOPIC, missing_ok=True)

    # A chat-born task comes home cancelled - from the one cancel door, so a HELD
    # task that never enqueued a workflow still settles its card (found in review: the card
    # spun forever). The report is idempotent per turn; the workflow's own exit may add nothing.
    if task.origin_conversation_id is not None:
        from prismoid.operations import chat as chat_ops

        said = await stopped.account(session, task)  # the stop's account, as the cancel frame carries it
        await chat_ops.on_task_settled(session, task, outcome="cancelled", summary=said)

    logfire.info("task {task} cancelled", task=str(task.id))
    return task.status


async def update_meta(
    session: AsyncSession,
    actor: Actor,
    task_id: uuid.UUID,
    *,
    title: str | None = None,
    pinned: bool | None = None,
) -> Task:
    """The user-space surface: label and pin. Neither is a state transition - the machine's
    states stay the machine's. The creator names and pins their own work in any state.

    A title change lands in the record as `task.renamed` (a label the record never saw never
    happened). A pin does not: it is per-list triage, view preference rather than work.
    """
    task = await Task.get_for(session, task_id, actor)  # scope: only a task you can see
    if task.creator_actor_id != actor.id:
        raise _forbidden("only the task's creator can rename or pin it")

    if title is not None and task.title != title:
        task.title = title
        seq = await task.next_seq(session)
        await TaskEvent.append(
            session,
            TaskRenamed(
                seq=seq,
                task_id=TaskId(task.id),
                project_id=ProjectId(task.project_id) if task.project_id else None,
                at=utcnow(),
                actor=actor_ref(actor),
                title=title,
            ),
        )
    if pinned is not None:
        task.pinned_at = utcnow() if pinned else None

    await session.commit()
    return task


async def move(
    session: AsyncSession, actor: Actor, task_id: uuid.UUID, to_project_id: uuid.UUID | None
) -> str:
    """Move a task in, out, or between projects - **and never rewrite the ledger.**

    The authorisation is two-sided: the **creator** may move their own task, and moving it *into* a
    project needs the **editor** role *there* (you cannot file work in a folder you may only read). To
    the project it leaves, a move is just a departure - a creator can always take their own task home.

    **The ledger law, and it is the whole point of this verb.** `usage_events` are attributed at the
    moment of spend - `project_id` is a plain column stamped when the money leaves, not a join through
    the task's *current* project. So the four dollars this task already spent while it was personal stay
    personal, the spend after it joins a project rolls to that project, and moving it back `--personal`
    does not retroactively un-bill the project for what it paid for. A move changes where *future* work
    is filed; it cannot rewrite where past money went, because that would let a task launder its cost
    from one budget to another after the fact.
    """
    from prismoid.db.models.project import Project, Role

    task = await Task.get_for(session, task_id, actor)  # scope: you can move only a task you can see
    if task.creator_actor_id != actor.id:
        raise _forbidden("only the task's creator can move it")

    # **A move is filing, not a state transition** - so a *finished* task can be moved too, to share a
    # completed piece of work into a team project. Its past spend stays where it was billed (the ledger
    # law below), which is precisely what keeps the project's budget from absorbing a cost it never
    # authorised. Moving a running task redirects only its *future* spend.

    destination: Project | None = None
    if to_project_id is not None:
        destination = await Project.get_for(session, to_project_id, actor, min_role=Role.EDITOR)

    # The row lock BEFORE anything below reads the task: `get_for` was an unlocked read, and a
    # planning pass may be mid-`record` on this very row. Waiting here means the Project we move
    # from and the pack we fence are the committed, current ones - not the copies from before
    # planning wrote. Same house pattern as `continue_task`'s lock, at
    # `Task.hold`'s strength (NO KEY UPDATE: it queues `next_seq`, never a child row's insert).
    await session.refresh(task, with_for_update={"key_share": True})

    from_project_id = task.project_id
    if from_project_id == to_project_id:
        return task.status  # a move to where it already is: a no-op, not an error

    await device_grants.keep_move_private(session, task, destination)

    task.project_id = to_project_id
    await context.fence_for_project(session, task, to_project_id)  # what its new readers may read
    seq = await task.next_seq(session)
    await TaskEvent.append(
        session,
        TaskMoved(
            seq=seq,
            task_id=TaskId(task.id),
            project_id=ProjectId(to_project_id) if to_project_id else None,
            at=utcnow(),
            actor=actor_ref(actor),
            from_project_id=ProjectId(from_project_id) if from_project_id else None,
            to_project_id=ProjectId(to_project_id) if to_project_id else None,
        ),
    )
    await session.commit()
    logfire.info("task {task} moved to project {project}", task=str(task.id), project=str(to_project_id))
    return task.status


@dataclass(frozen=True, slots=True)
class BudgetSet:
    usd: Decimal
    previous_usd: Decimal | None
    gate_id: uuid.UUID | None  # the task's open budget gate: a caller that resumes resolves it


async def set_budget(session: AsyncSession, actor: Actor, task_id: uuid.UUID, usd: Decimal) -> BudgetSet:
    """Set the task's USD ceiling (B3): the answer "Budget reached" asks for. An editor of the
    task's Project, or the creator of a task with no Project (`require_project_editor`). The task row lock
    serialises it with every other task writer. The gate is NOT resolved here; resuming is its
    accept, which dispatches the parked wave with no re-check (the NEXT boundary checks), so a raise
    that still falls short runs one more wave and parks again. While "Budget reached" is open, a
    raise never goes below the current ceiling (422): resuming then runs the wave past the amount
    just typed. With no such gate open the budget may go down. A Project ceiling is not this: only the
    Project's owner raises it, and while the task's Project is over, no "Budget reached" accept resumes
    it, whichever ceiling the gate names (`gates.resolve`)."""
    from prismoid.db.models.gate import Gate
    from prismoid.errors import ValidationFailed
    from prismoid.operations.gates import BUDGET_TITLE, budget_of, person_cap

    # Dollars and cents, once: "1e2" was stored as "1E+2", and the row, the event,
    # the response and the Bursar's reason ("$1E+2") all repeated it.
    usd = usd.quantize(Decimal("0.01"))
    task = await Task.get_for(session, task_id, actor)  # scope: you can raise only a task you can see
    await require_project_editor(session, task, actor)
    await session.refresh(task, with_for_update={"key_share": True})
    previous = budget_of(task.budget)
    gate = await Gate.open_budget_for(session, task.id)
    if gate is not None and (gate.spec or {}).get("title") != BUDGET_TITLE:
        # Fix wave B4: only "Budget reached" is a raise's to resume. The long-form cost confirmation is a
        # budget gate too, and accepting it is consent to the whole document and its figure fleet. The title,
        # not `budget_scope`: a gate parked before the scope existed carries none and must stay resumable.
        gate = None
    if gate is not None and previous is not None and usd < previous:
        raise ValidationFailed(
            f"${usd} would lower the ${previous} budget while the task waits on it; "
            f"raise it to at least ${previous}, or cancel the task",
            fields={"usd": f"at least {previous}"},
        )
    task.budget = person_cap(task.budget, usd)  # a cap a person sets is theirs, never the default
    seq = await task.next_seq(session)
    await TaskEvent.append(
        session,
        TaskBudgetSet(
            seq=seq,
            task_id=TaskId(task.id),
            project_id=ProjectId(task.project_id) if task.project_id else None,
            at=utcnow(),
            actor=actor_ref(actor),
            usd=usd,
            previous_usd=previous,
        ),
    )
    await session.commit()
    return BudgetSet(usd=usd, previous_usd=previous, gate_id=gate.id if gate else None)


def _forbidden(message: str) -> Exception:
    from prismoid.errors import Forbidden

    return Forbidden(message)


async def require_project_editor(session: AsyncSession, task: Task, actor: Actor) -> None:
    """A consequential action on a PROJECT task needs the editor role THERE:
    steer, pause, resume, cancel are editor+, exactly like `move`/`revise`/`follow_up`.

    `Task.get_for` is visibility only - a project *viewer* can SEE the task and its stream but must
    not steer or cancel it. A spaceless (solo) task is its creator's alone and is unaffected.
    `Project.get_for(min_role=EDITOR)` raises `NotFound` for a stranger and `Forbidden` for a
    viewer, the same denial `move` (~223) already makes on a move's destination.
    """
    if task.project_id is None:
        return
    from prismoid.db.models.project import Project, Role

    await Project.get_for(session, task.project_id, actor, min_role=Role.EDITOR)


async def may_edit(session: AsyncSession, task: Task, actor: Actor) -> bool:
    """The same check every task write makes, as a boolean for the UI (`TaskDetailOut.can_edit`).
    A personal task is its creator's; a Project task wants editor+."""
    if task.project_id is None:
        return task.creator_actor_id == actor.id
    try:
        await require_project_editor(session, task, actor)
    except (Forbidden, NotFound):
        # NotFound: a creator removed from the Project still SEES their task (`Task._visible_to`)
        # but holds no role there - a boolean for the UI says "read-only", never a 404.
        return False
    return True


async def may_end_control(session: AsyncSession, task: Task, actor: Actor) -> bool:
    """Who may end ANYONE's control of the task's browser: the owner of the
    task's Project, or on a personal task its creator. One rule, two readers: `TaskDetailOut.
    can_end_control` (the pane's End {name}'s control) and the screen release that ends a lease
    someone else holds - so the button is never offered where the server would refuse."""
    if task.project_id is None:
        return task.creator_actor_id == actor.id
    from prismoid.db.models.project import Project

    project = await session.get(Project, task.project_id)
    return project is not None and project.owner_actor_id == actor.id


async def _advance(session: AsyncSession, task: Task, to: TaskStatus, *, actor: Actor | None = None) -> None:
    task.transition(to)
    seq = await task.next_seq(session)
    await TaskEvent.append(
        session,
        TaskStatusChanged(
            seq=seq,
            task_id=TaskId(task.id),
            project_id=ProjectId(task.project_id) if task.project_id else None,
            at=utcnow(),
            actor=actor_ref(actor) if actor else None,
            status=to,
            summary=await stopped.account(session, task) if to is TaskStatus.CANCELLED else "",
        ),
    )


def _finished(task: Task, verb: str) -> Exception:
    return ValidationFailed(
        f"cannot {verb} a task that is already {task.status} - send a follow-up to continue it"
    )


def _wrong_state(task: Task, verb: str) -> Exception:
    return ValidationFailed(f"cannot {verb} a task that is {task.status}")
