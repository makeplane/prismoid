# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The task verbs. Operations own transactions and compose everything else.

`create_task` is the first lifecycle of a task, and the order of its four steps is the whole design:

    scope-check the Project → insert the Task → append task.created (+ pg_notify) → COMMIT
                                                                                    ↓
                                                            enqueue `task-{id}` - AFTER the commit

**Enqueue after commit, never inside it.** A job enqueued inside the transaction can be picked up
by a worker before the transaction commits - and the worker then reads a task that does not exist
yet. The window is milliseconds and the bug is a ghost, which is the worst kind. Enqueueing after
the commit trades that ghost for a different one (a crash between commit and enqueue leaves a
task nobody started), and *that* ghost is cheap to kill: the sweeper re-enqueues `created`
tasks older than 60 s, and the deterministic job id makes the double-enqueue harmless.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

import logfire
from sqlalchemy import delete as sql_delete
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid import storage
from prismoid.db.base import utcnow
from prismoid.db.models.actor import Actor
from prismoid.db.models.artifact import Artifact
from prismoid.db.models.event import REPLAY_PAGE, TaskEvent
from prismoid.db.models.message import Message
from prismoid.db.models.project import Project
from prismoid.db.models.task import Task
from prismoid.db.models.user import User
from prismoid.errors import Conflict, Forbidden, PolicyDenied, ValidationFailed
from prismoid.operations import allowance, device_grants, machines, planning_steps, sources, task_door
from prismoid.operations.attribution import actor_ref  # re-exported: callers import it from here
from prismoid.operations.device_gates import device_wait
from prismoid.operations.dispatch import get_dispatcher, task_job_key, task_turn_job_key
from prismoid.operations.turn.dials import TurnDials, budget_changed, merge_into, refuse_shadowing
from prismoid.schemas.contract import BotPin
from prismoid.schemas.events import DurableEvent, TaskContinued, TaskCreated, TaskFeedback, redact_for
from prismoid.schemas.ids import ProjectId, TaskId
from prismoid.schemas.inputs import MAX_INPUT_BYTES, MAX_INPUTS, InputSet, RawInput, TaskInput, not_a_source
from prismoid.schemas.machines import MachineSummary
from prismoid.schemas.plan import PlanView, TaskMode, TaskStatus
from prismoid.schemas.principal import CredKind
from prismoid.schemas.usage import CostSummary


def input_download_url(item: dict[str, object]) -> str:
    """A presigned link for a task INPUT - same content-addressed store, same public
    endpoint as an artifact download; inputs just never went out the door before
    the transcript grew attachment chips."""
    return storage.presign(str(item["sha256"]), str(item["filename"]))


async def store_inputs(raw: Sequence[RawInput | TaskInput]) -> InputSet:
    """Bytes → the content-addressed object store, names + digests → the row (`storage.py`); a
    `TaskInput` is already stored (a chat line's file), so its digest rides as is."""
    if len(raw) > MAX_INPUTS:
        raise PolicyDenied(f"at most {MAX_INPUTS} inputs per task; got {len(raw)}")

    items: list[TaskInput] = []
    seen: set[str] = set()
    for item in raw:
        if item.filename in seen:
            # Two inputs, one filename → one of them silently overwrites the other in the frame.
            raise PolicyDenied(f"{item.filename} is attached twice; filenames are the frame's paths")
        seen.add(item.filename)
        if isinstance(item, TaskInput):
            items.append(item)
            continue
        if len(item.data) > MAX_INPUT_BYTES:
            raise PolicyDenied(
                f"{item.filename}: {len(item.data):,} bytes exceeds the {MAX_INPUT_BYTES:,} limit"
            )
        if (why := not_a_source(item.data)) is not None:
            raise PolicyDenied(f"{item.filename} {why}")

        items.append(
            TaskInput(
                filename=item.filename,
                # `storage.put` is a blocking boto3 call - off the event loop (Rule 5's spirit), or
                # a multi-MB attachment stalls the whole worker for the length of the upload.
                sha256=await asyncio.to_thread(storage.put, item.data),
                bytes=len(item.data),
                media_type=item.media_type,
            )
        )
    return InputSet(items=items)


async def create_task(
    session: AsyncSession,
    actor: Actor,
    *,
    goal: str,
    project_id: uuid.UUID | None = None,
    mode: TaskMode = TaskMode.STANDARD,
    clarify: str = "auto",
    budget: dict[str, object] | None = None,
    parent_task_id: uuid.UUID | None = None,
    inputs: Sequence[RawInput | TaskInput] = (),
    skills: Sequence[str] = (),
    connectors: Sequence[str] = (),
    bots: Sequence[str] = (),
    direct: bool = False,
    models: Sequence[str] = (),
    deliverable: str | None = None,
    deliverable_source: str | None = None,  # fork-only (inherit_mode): never wire-fed
    deliverable_format: str | None = None,
    quality: str | None = None,
    scale: str = "auto",
    target_pages: int | None = None,
    client_token: str | None = None,
    title: str | None = None,
    hold: bool = False,
    inherit_mode: bool = False,
    origin_routine_id: uuid.UUID | None = None,
    origin_conversation_id: uuid.UUID | None = None,
    origin_message_seq: int | None = None,
    origin_depth: int = 0,
    parent_seq: int | None = None,  # fork-only: the parent's event_seq at the branch point
    folder: str | None = None,
    docker: bool = False,
    credential: CredKind | None = None,  # the asker's credential kind, at a task door only
) -> Task:
    # The creator-is-human invariant, enforced not merely narrated: `creator_actor_id` targets `actors`, and
    # only a human satellite may hold it - an agent/service actor cannot own a task (a fork inherits this path).
    if not actor.is_human():
        raise Forbidden("only a human actor can own a task")

    # **The idempotency guardrail**: the same send gesture replayed - a held Enter, an HTTP retry, a script rerun -
    # must return the SAME task, never a twin. The pre-select absorbs every practical replay; the partial unique
    # index on (creator, client_token) makes the same-millisecond race a constraint error, still never a
    # duplicate. Checked before any side effect (no orphaned stored bytes).
    if client_token is not None:
        existing = await Task.by_client_token(session, actor.id, client_token)
        if existing is not None:
            return existing

    # **Creating a task *in a project* needs the editor role there**. A viewer can read the project's tasks but
    # not add to it; a stranger cannot even see it. `get_for(min_role=EDITOR)` is the one gate, raised before any
    # attachment is stored - a task that could not be created must not leave orphaned bytes in the object store.
    project: Project | None = None
    if project_id is not None:
        from prismoid.db.models.project import Role

        project = await Project.get_for(session, project_id, actor, min_role=Role.EDITOR)

    # **The device**: checked at the door, before anything is stored.
    placement = await device_grants.place_task(session, actor, project, folder=folder, docker=docker)

    mode, deliverable = await task_door.create_dials(
        session,  # the door (`task_door`): the allowance first, then the dials; a refusal stores nothing
        actor,
        project_id=project_id,
        mode=mode,
        models=models,
        deliverable=deliverable,
        deliverable_format=deliverable_format,
        scale=scale,
        inherit_mode=inherit_mode,
    )

    # Before the row, not after: an attachment that cannot be stored must not leave a task behind
    # that references it. The object store is content-addressed, so a re-submitted attachment on a
    # retried request is the same key - storing first is idempotent, and storing second is not safe.
    attachments = await store_inputs(inputs)

    # **Skill refs → version-pinned pins, at submit time**. Resolved before the row so a
    # bad ref fails the submit rather than surfacing as a mid-run planning error - and so the version
    # is fixed now, which is the reproducibility the pins exist for.
    from prismoid.operations import skills as skill_ops

    pins = [await skill_ops.resolve_ref(session, ref, actor_id=actor.id) for ref in skills]

    connector_ids, connectors_auto, connectors_auto_ids = await task_door.create_connectors(
        session, actor, goal, connectors, inherit=inherit_mode, routine=origin_routine_id is not None
    )

    bot_pins = await _bot_refs(session, actor, project_id, bots, direct=direct)

    # Last before the row, so the locks it takes are held only to the commit.
    await device_grants.hold_place(session, actor, project, placement)

    task = Task(
        id=uuid.uuid4(),  # client-side, because `task-{id}` must exist before the commit
        goal=goal,
        project_id=project_id,
        creator_actor_id=actor.id,
        turn_actor_id=actor.id,  # the first turn's author is the asker
        plan_epoch=1,  # the first turn opens epoch 1, authored by the asker
        turn_epoch=1,
        epoch_actor_id=actor.id,
        parent_task_id=parent_task_id,
        mode=mode.value,
        clarify=clarify,
        budget=task_door.with_default_cap(budget),
        criteria={},
        inputs=[i.model_dump(mode="json") for i in attachments.items],
        skill_pins=[p.model_dump(mode="json") for p in pins],
        connector_ids=connector_ids,
        connectors_auto=connectors_auto_ids,
        bot_pins=[p.model_dump(mode="json") for p in bot_pins],
        direct_bot=str(bot_pins[0].name) if direct else None,
        model_refs=list(models) or None,
        deliverable=deliverable,
        # WHO chose the kind: a fork inherits its parent's answer verbatim (a
        # plan-stamped parent kind must not become a fake user dial on the child); a wire
        # deliverable is the dial; anything else resolves at planning (auto/plan).
        deliverable_source=(
            deliverable_source if inherit_mode else ("user" if deliverable is not None else None)
        ),
        deliverable_format=deliverable_format,
        quality=quality,  # the requirement dial; the serializer's Literal is the validation
        # Local mode runs every task's Machine on this computer.
        machine_driver=placement.machine_driver,
        # Stored RAW: "auto" is a real state until intake detection resolves it -
        # an explicit "standard" must stay distinguishable (it NEVER enters the lane).
        scale=scale,
        target_pages=target_pages,
        client_token=client_token,
        title=title,
        held_at=utcnow() if hold else None,
        # Routine provenance: the machine origin; the creator above stays the
        # routine's human owner - attribution never forks.
        origin_routine_id=origin_routine_id,
        # Chat provenance: the conversation and the message that started this work.
        origin_conversation_id=origin_conversation_id,
        origin_message_seq=origin_message_seq,
        origin_depth=origin_depth,
    )
    session.add(task)
    await session.flush()
    await device_grants.record_place(session, task, actor, placement)

    await _journal_created(
        session, task, actor, credential, parent_seq=parent_seq, connectors_auto=connectors_auto
    )

    await session.commit()

    # ── the boundary. Everything above is durable; everything below is best-effort. ──
    if task.held_at is None:  # a HELD fork launches from its first directive, not from birth
        await get_dispatcher().enqueue("run_task", task_job_key(task.id), task_id=str(task.id))
    return task


async def _bot_refs(
    session: AsyncSession, actor: Actor, project_id: uuid.UUID | None, bots: Sequence[str], *, direct: bool
) -> list[BotPin]:
    """A submit's bots, pinned to versions; direct mode demands exactly one."""
    # **Bot refs → version-pinned pins, at submit time** (the skill-pin shape again). Naming bots here is an
    # OVERRIDE, not the only way to get them: the roster is auto-included from the task's Project and the
    # creator's own published bots, because a roster you must remember to attach is not a roster. This list is
    # how you pin an author-only draft (`reviewer@draft`) or narrow a crowded space to the two that matter.
    from prismoid.operations import bots as bot_ops

    bot_pins = [await bot_ops.resolve_ref(session, ref, actor=actor, project_id=project_id) for ref in bots]

    # **Direct mode**: exactly one bot, and it answers every turn - planning takes the
    # code-made one-step path, the planner structurally unreachable. Demanding the pin here means
    # a typo'd bot name is a 404 at the door, never a task that plans into thin air.
    if direct and len(bot_pins) != 1:
        raise ValidationFailed(
            "direct mode needs exactly one bot",
            fields={"bots": "pass the one bot this task talks to"},
        )
    return bot_pins


async def _journal_created(
    session: AsyncSession,
    task: Task,
    actor: Actor,
    credential: CredKind | None,
    *,
    parent_seq: int | None,
    connectors_auto: list[str],
) -> None:
    """`task.created` for a row just flushed: its goal, mode and lineage as the row holds them."""
    seq = await task.next_seq(session)
    await TaskEvent.append(
        session,
        TaskCreated(
            seq=seq,
            task_id=TaskId(task.id),
            project_id=ProjectId(task.project_id) if task.project_id else None,
            at=utcnow(),
            actor=actor_ref(actor),
            goal=task.goal,
            mode=TaskMode(task.mode),
            parent_task_id=TaskId(task.parent_task_id) if task.parent_task_id else None,
            parent_seq=parent_seq,
            connectors_auto=connectors_auto,
            credential=credential,
        ),
    )


async def replan(session: AsyncSession, actor: Actor, task_id: uuid.UUID, *, reason: str) -> int:
    """Re-plan the turn's unstarted steps in the editor's words and run them; returns the plan's step count.
    The per-turn job id (`task-{id}-t{n}`) dedupes against the still-running workflow."""
    from prismoid.operations.turn import replan as turn_replan  # cycle: the re-plan reaches the engine

    turn, steps = await turn_replan.by_editor(session, actor, task_id, reason)
    await get_dispatcher().enqueue(
        "run_task", task_turn_job_key(task_id, turn), task_id=str(task_id), turn=turn
    )
    return steps


async def _continued_with(session: AsyncSession, task_id: uuid.UUID, token: str) -> bool:
    """Has any turn of this task already been opened by this send gesture? Scans the task's own
    `task.continued` events (bounded by its turn count) rather than only the newest - a delayed
    HTTP retry can arrive after a *later* turn opened, and it must still be absorbed as the
    replay it is, never 409ed as "live"."""
    events = await TaskEvent.of_types(session, task_id, ("task.continued",))
    return any(isinstance(e, TaskContinued) and e.client_token == token for e in events)


async def _live_conflict(session: AsyncSession, task: Task) -> Conflict:
    """A follow-up that lost the race hears who won, so it can steer instead.
    A NULL turn author reads as the creator, as everywhere else (the migration's promise)."""
    winner = await session.get(Actor, task.turn_actor_id or task.creator_actor_id)
    who = winner.display_name if winner is not None else "Someone"
    return Conflict(f"{who} just started the next turn - send yours as a steer")


async def continue_task(
    session: AsyncSession,
    actor: Actor,
    task_id: uuid.UUID,
    *,
    goal: str,
    clarify: str = "auto",
    budget: dict[str, object] | None = None,
    inputs: Sequence[RawInput | TaskInput] = (),
    skills: Sequence[str] = (),
    connectors: Sequence[str] = (),
    bots: Sequence[str] = (),
    models: Sequence[str] = (),
    deliverable: str | None = None,
    deliverable_format: str | None = None,
    quality: str | None = None,
    scale: str | None = None,
    target_pages: int | None = None,
    client_token: str | None = None,
    credential: CredKind | None = None,  # the sender's credential kind, at the follow-up door only
) -> Task:
    """A follow-up continues the task (same row, next turn): a settled task re-enters `planning` with the
    dials merged; a live one is a 409 (steer instead); `client_token` makes a replay return the same turn."""
    task = await Task.get_for(session, task_id, actor)  # scope first
    if task.project_id is not None:
        from prismoid.db.models.project import Project, Role

        await Project.get_for(session, task.project_id, actor, min_role=Role.EDITOR)
    if task.archived_at is not None:
        raise Conflict("unarchive the task before continuing it")

    # Honest routing before any side effect: a live task is steered, not continued - unless
    # this is the very gesture that already opened a turn, replayed.
    if not task.is_terminal():
        if client_token is not None and await _continued_with(session, task.id, client_token):
            return task
        raise await _live_conflict(session, task)

    await allowance.require(session, task.workspace_id, actor=actor)  # a turn is new spend (ADR-103)
    # A device task's folder taken meanwhile, read early so a refusal stores nothing; decided again
    # under the row lock below.
    await device_grants.reclaim_folders(session, actor, task, lock=False)

    dials = await _turn_dials(
        session,
        actor,
        task,
        inputs=inputs,
        skills=skills,
        bots=bots,
        connectors=connectors,
        models=models,
        deliverable=deliverable,
        deliverable_format=deliverable_format,
    )

    # The row lock. Re-read state under it - this is what serializes two racing continues:
    # the loser re-checks and sees either its own replayed token (202) or a live task (409).
    await device_grants.lock_task(session, task)
    if client_token is not None and await _continued_with(session, task.id, client_token):
        return task
    if not task.is_terminal():
        raise await _live_conflict(session, task)
    # A device task's folder may have been taken while it was settled.
    await device_grants.reclaim_folders(session, actor, task)

    # Belt: a settled task has no open gates by construction (cancel closes them, delivery expires them, intake
    # times out) - but a stray one parked forever would eat the first wake of the new turn.
    from prismoid.operations import gates as gate_ops

    await gate_ops.expire_open(session, task)

    cap_before = None if task.budget is None else dict(task.budget)
    cap_changed = budget_changed(task.budget, budget)
    merge_into(
        task,
        dials,
        budget=budget,
        models=models,
        deliverable_format=deliverable_format,
        quality=quality,
        scale=scale,
        target_pages=target_pages,
    )
    task.clarify = clarify  # recorded stance; turns skip the clarifier regardless

    task.turn += 1
    task.turn_actor_id = actor.id  # the follow-up's sender authors this turn
    task.plan_epoch += 1  # a turn's start opens an epoch, authored by its sender
    task.turn_epoch = task.plan_epoch
    task.epoch_actor_id = actor.id
    task.transition(TaskStatus.PLANNING)
    seq = await task.next_seq(session)
    await TaskEvent.append(
        session,
        TaskContinued(
            seq=seq,
            task_id=TaskId(task.id),
            project_id=ProjectId(task.project_id) if task.project_id else None,
            at=utcnow(),
            actor=actor_ref(actor),
            message=goal,
            turn=task.turn,
            epoch=task.plan_epoch,
            client_token=client_token,
            # THIS turn's dials, None = untouched: planning reads the event, never the row's stamp.
            scale=scale,
            target_pages=target_pages,
            deliverable=dials.deliverable,  # same law: the turn's dial, never the stamp
            deliverable_format=deliverable_format,
            attached=[i.filename for i in dials.attachments.items],
            credential=credential,
            budget_before=cap_before if cap_changed else None,
            budget_after=dict(task.budget) if cap_changed and task.budget is not None else None,
        ),
    )
    await session.commit()

    # ── the boundary. The per-turn id: a completed workflow id never re-executes. ──
    await get_dispatcher().enqueue(
        "run_task", task_turn_job_key(task.id, task.turn), task_id=str(task.id), turn=task.turn
    )
    logfire.info("task {task} continued into turn {turn}", task=str(task.id), turn=task.turn)
    return task


async def _turn_dials(
    session: AsyncSession,
    actor: Actor,
    task: Task,
    *,
    inputs: Sequence[RawInput | TaskInput],
    skills: Sequence[str],
    bots: Sequence[str],
    connectors: Sequence[str],
    models: Sequence[str],
    deliverable: str | None,
    deliverable_format: str | None,
) -> TurnDials:
    """Resolve and validate a follow-up's dials, holding no lock and committing nothing. These are
    the slow, lock-free side effects: `next_seq` holds the row lock to COMMIT, and an S3 put or a
    catalog read under that lock would serialize the world."""
    attachments = await store_inputs(inputs)
    refuse_shadowing({str(i["filename"]) for i in task.inputs}, attachments)
    from prismoid.operations import skills as skill_ops

    pins = [await skill_ops.resolve_ref(session, ref, actor_id=actor.id) for ref in skills]
    from prismoid.operations import bots as bot_ops

    bot_pins = [
        await bot_ops.resolve_ref(session, ref, actor=actor, project_id=task.project_id) for ref in bots
    ]
    connector_ids: list[str] = []
    if connectors:
        from prismoid.operations import connectors as connector_ops

        # A fresh-turn dial like skills/models: validated at the door, merged below.
        # CREATOR-ONLY: a task mounts its creator's credentials, so a co-editor's refs would
        # union ids the mount must refuse - an honest 422 here instead of a silent drop there.
        if actor.id != task.creator_actor_id:
            raise ValidationFailed(
                "only the task's creator can attach connectors",
                fields={
                    "connectors": "a connector mounts its owner's credential - the task's creator must attach it"
                },
            )
        connector_ids = await connector_ops.validate_refs(session, actor, list(connectors))
    return TurnDials(
        attachments=attachments,
        skill_pins=[p.model_dump(mode="json") for p in pins],
        bot_pins=[p.model_dump(mode="json") for p in bot_pins],
        connector_ids=connector_ids,
        deliverable=await task_door.turn_dials(
            session, task, models=models, deliverable=deliverable, deliverable_format=deliverable_format
        ),
    )


async def retry_task(
    session: AsyncSession,
    actor: Actor,
    task_id: uuid.UUID,
    *,
    client_token: str | None = None,
) -> Task:
    """Run the latest ask AGAIN - the Retry button's verb: a new turn carrying the
    same message, planned with the whole thread in hand - including what just went wrong, which
    the transcript shows the planner honestly. Works on delivered ("try that again"), failed
    and cancelled alike; a live task refuses toward steering, and the client_token makes a
    double-press one retry, never two."""
    task = await Task.get_for(session, task_id, actor)  # scope first
    if not task.is_terminal():
        if client_token is not None and await _continued_with(session, task.id, client_token):
            return task  # the very press that opened this turn, replayed
        raise await _live_conflict(session, task)
    latest = await TaskEvent.latest_of_type(session, task.id, "task.continued")
    ask = latest.message if isinstance(latest, TaskContinued) else task.goal
    return await continue_task(session, actor, task_id, goal=ask, client_token=client_token)


async def record_feedback(session: AsyncSession, actor: Actor, task_id: uuid.UUID, *, rating: str) -> Task:
    """The thumbs: one durable `task.feedback` frame per press. Append-only like
    every judgment in the record - a changed mind is a second frame, and the fold keeps the
    newest. Never machine state: grading an answer moves nothing; it is remembered. A write,
    so a Project viewer may not grade (viewers watch)."""
    from prismoid.operations import steering

    task = await Task.get_for(session, task_id, actor)  # scope first
    await steering.require_project_editor(session, task, actor)
    if rating not in ("up", "down"):
        raise ValidationFailed("rating must be 'up' or 'down'")
    seq = await task.next_seq(session)
    await TaskEvent.append(
        session,
        TaskFeedback(
            seq=seq,
            task_id=TaskId(task.id),
            project_id=ProjectId(task.project_id) if task.project_id else None,
            at=utcnow(),
            actor=actor_ref(actor),
            rating=rating,
            turn=task.turn,
        ),
    )
    await session.commit()
    logfire.info("task {task} rated {rating} (turn {turn})", task=str(task.id), rating=rating, turn=task.turn)
    return task


async def fork(
    session: AsyncSession,
    actor: Actor,
    parent_id: uuid.UUID,
    *,
    client_token: str | None = None,
    folder: str | None = None,
) -> Task:
    """Branch a task: a new child, born held, carrying the parent's thread, artifacts (read-only) and dials;
    the first steer launches it. A Machine seeds from `/work` only; a device fork copies no folder."""
    parent = await Task.get_for(session, parent_id, actor)  # scope first
    label = (parent.title or parent.goal).strip()
    while label.startswith("[Forked] "):  # forking a fork wears ONE prefix, never a stack
        label = label.removeprefix("[Forked] ")
    # Connectors inherit FILTERED to what still mounts: a connector deleted or dead
    # since the parent's submit costs the capability, never the branch - the same law as a
    # since-disabled fleet seat, decided at the door instead of as a 422 in the user's face.
    from prismoid.operations import connectors as connector_ops

    live_connectors = await connector_ops.filter_live(session, actor, list(parent.connector_ids))
    child = await create_task(
        session,
        actor,
        goal=parent.goal,
        project_id=parent.project_id,
        mode=TaskMode(parent.mode),
        clarify="never",  # the branch launches on its first directive - a clarifier gate
        # before that would be a dialog in costume (user law v4)
        budget=dict(parent.budget) if parent.budget else None,
        parent_task_id=parent.id,
        # The branch point: the fork reads the parent's thread up to here, never the turns it takes afterwards
        # - a teammate's fork of a task its creator later works on alone must not keep reading her.
        parent_seq=parent.event_seq,
        skills=[f"{p['name']}@{p['version']}" for p in parent.skill_pins],
        # The parent's bots ride along too (a fork carries the parent's dials). Pinned to
        # the same versions: a fork is a branch of this conversation, not a fresh read of the
        # roster, so it must keep running the agents the parent was running.
        bots=[f"{p['name']}@{p['version']}" for p in parent.bot_pins],
        connectors=live_connectors,
        models=list(parent.model_refs or []),
        deliverable=parent.deliverable,
        deliverable_source=parent.deliverable_source,  # verbatim: a plan stamp stays a plan stamp
        deliverable_format=parent.deliverable_format,
        quality=parent.quality,  # a dial is thread-DNA on a branch
        scale=parent.scale,
        target_pages=parent.target_pages,
        client_token=client_token,
        title=f"[Forked] {label}"[:60],
        hold=True,
        inherit_mode=True,
        folder=folder,
    )
    await sources.inherit(parent.id, child.id)  # a revision cites the parent's pages by their ids
    await machines.seed_fork(session, parent, child)  # files, never logins
    return child


async def get_task(session: AsyncSession, actor: Actor, task_id: uuid.UUID) -> Task:
    return await Task.get_for(session, task_id, actor)


@dataclass(frozen=True)
class TaskRead:
    """A task as its detail page reads it, from ONE operation (the one-call view rule):
    the row, its plan and spend, what the CALLER may do to it (`can_edit` the
    writes, `can_curate` archive/delete, `is_creator` rename/pin and a connector approval's
    Allow), and the creator's name, so a teammate's approval dock can say who it waits on."""

    task: Task
    plan: PlanView
    cost: CostSummary
    can_edit: bool
    can_curate: bool
    is_creator: bool
    creator_name: str | None
    machine: MachineSummary | None = None
    can_end_control: bool = False  # may end anyone's control of the browser
    waiting_for: str | None = None  # a step waiting for its device: "Waiting for <device name>"


async def read_task(session: AsyncSession, actor: Actor, task_id: uuid.UUID) -> TaskRead:
    from prismoid.operations import steering
    from prismoid.operations import usage as usage_ops

    task = await Task.get_for(session, task_id, actor)
    creator = (await User.by_actor_ids(session, [task.creator_actor_id])).get(task.creator_actor_id)
    return TaskRead(
        task=task,
        plan=await planning_steps.plan_view(session, task),
        cost=await usage_ops.for_task(session, task.id),
        can_edit=await steering.may_edit(session, task, actor),
        can_curate=await may_curate(session, task, actor),
        is_creator=task.creator_actor_id == actor.id,
        creator_name=(creator.name or None) if creator is not None else None,
        machine=await machines.summary(session, task.id),
        can_end_control=await steering.may_end_control(session, task, actor),
        waiting_for=await device_wait(session, task),
    )


async def may_curate(session: AsyncSession, task: Task, actor: Actor) -> bool:
    """The curation rule, one place: the task's creator, or the owner of the project it is filed in -
    you curate your own work, and a project owner may tidy their own room; a mere member or
    editor cannot curate another person's task. `_require_curator` raises on it, and the detail
    read hands it to the UI as `TaskDetailOut.can_curate`, so the menu never offers a verb the
    server would refuse."""
    if task.creator_actor_id == actor.id:
        return True
    if task.project_id is None:
        return False
    project = await session.get(Project, task.project_id)
    return project is not None and project.owner_actor_id == actor.id


async def _require_curator(session: AsyncSession, task: Task, actor: Actor, *, verb: str) -> None:
    """Delete + archive speak `may_curate`."""
    if not await may_curate(session, task, actor):
        raise Forbidden(f"only the task's creator or the project owner can {verb} it")


async def archive_task(session: AsyncSession, actor: Actor, task_id: uuid.UUID) -> Task:
    """Archive - the pin's sibling (user-space triage, never a machine status, and like
    the pin it writes NO task event): the row leaves default lists but keeps every event,
    artifact, and ledger entry, and unarchive restores it whole. **Terminal-only**, exactly
    like delete - archiving hides work, and hiding a RUNNING task would bury a machine
    mid-flight (the 409 says cancel first). Idempotent: re-archiving keeps the first stamp."""
    task = await Task.get_for(session, task_id, actor)
    await _require_curator(session, task, actor, verb="archive")
    if not task.is_terminal():
        raise Conflict("cancel the task before archiving it")
    if task.archived_at is None:
        task.archived_at = utcnow()
        await session.commit()
        logfire.info("task {task} archived by {actor}", task=str(task.id), actor=str(actor.id))
    return task


async def unarchive_task(session: AsyncSession, actor: Actor, task_id: uuid.UUID) -> Task:
    """The way back out of the drawer - same curators, no state requirement (an archived task
    is terminal by construction). Idempotent."""
    task = await Task.get_for(session, task_id, actor)
    await _require_curator(session, task, actor, verb="unarchive")
    if task.archived_at is not None:
        task.archived_at = None
        await session.commit()
        logfire.info("task {task} unarchived by {actor}", task=str(task.id), actor=str(actor.id))
    return task


async def delete_task(session: AsyncSession, actor: Actor, task_id: uuid.UUID) -> None:
    """Delete a task and everything it owns - **permanently**. The database's foreign keys do
    the careful part, and they were declared for exactly this:

    * **CASCADE** - the task's artifacts, `task_events`, plan (steps/edges/edits), gates, inbox
      items, and `memory_uses` are its own and go with it.
    * **SET NULL** - the billing ledger (`usage_events`), captures, minted memories
      (`source_task_id`), and any follow-up children (`parent_task_id`) keep their rows with the
      link nulled. Spend is attributed **at the time it happened** and a delete never rewrites the
      ledger; a user's memories outlive the task that taught them; a follow-up becomes a root
      task rather than vanishing with its parent.

    **Only a terminal task can be deleted.** A running task's workflow is still writing events, so
    pulling its row would crash it mid-flight - cancel first (a clear 409 says so). Authorised for
    the task's **creator or the owner of the project it is filed in**: you delete your own work, and
    a project owner may clean their own room (the owner outranks any membership; a mere member
    or editor cannot delete another person's task).
    """
    task = await Task.get_for(session, task_id, actor)  # scope first: visible, or NotFound
    await _require_curator(session, task, actor, verb="delete")

    if not task.is_terminal():
        raise Conflict("cancel the task before deleting it")

    # The blobs its artifacts point at, and its Machine - read BEFORE the cascade removes the rows.
    forgotten = await machines.forgettable(session, task.id)
    rows = await Artifact.for_task(session, task.id)  # every revision - GC walks the whole lineage
    digests = (
        {a.sha256 for a in rows}
        | {a.preview_sha256 for a in rows if a.preview_sha256}
        | {a.rendition_sha256 for a in rows if a.rendition_sha256}
    )

    # A direct SQL DELETE lets Postgres run the ON DELETE rules above; `session.delete(obj)` would
    # instead try to cascade in Python and needs mapped relationships we deliberately do not carry.
    await session.execute(sql_delete(Task).where(Task.id == task.id))
    await session.commit()
    logfire.info("task {task} deleted by {actor}", task=str(task.id), actor=str(actor.id))

    # The Machine's frame and every object under its prefix, on the worker: the API
    # never touches a frame. A failed enqueue leaves debris the reconciler and the orphan
    # arm reclaim - a storage cost, never a failed delete.
    if forgotten is not None:
        try:
            await get_dispatcher().enqueue(
                "machine_forget",
                f"machine-forget-{forgotten.machine_id}",
                machine_id=str(forgotten.machine_id),
                frame_ref=forgotten.frame_ref,
                driver=forgotten.driver,
            )
        except Exception:
            logfire.warn(
                "machine {m} not forgotten after task delete (enqueue failed)", m=str(forgotten.machine_id)
            )

    await sources.forget(task.id)  # the pages its web reads kept, under the task's own prefix
    # Best-effort blob GC, AFTER the rows are gone. Content-addressed dedup means a digest can
    # still back another task's artifact, a task's input or a chat line's file; deleting a shared blob
    # would strand it, so a blob is removed only when nothing surviving references it. Failure
    # here leaks a blob (a storage cost), never corrupts data - so it never fails the delete.
    held_by = (Artifact.any_with_digest, Task.any_input_with_digest, Message.any_input_with_digest)
    for digest in digests:
        if any([await held(session, digest) for held in held_by]):
            continue
        try:
            await asyncio.to_thread(storage.delete, digest)
        except Exception:
            logfire.warn("blob {d} left after task delete (store error)", d=digest[:12])


async def list_tasks(
    session: AsyncSession,
    actor: Actor,
    *,
    statuses: list[str] | None = None,
    project_id: uuid.UUID | None = None,
    mode: str | None = None,
    pinned: bool | None = None,
    archived: bool = False,
    q: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> tuple[list[Task], dict[uuid.UUID, str], dict[uuid.UUID, str]]:
    """A keyset page of the tasks the actor can see, the names of the people on it and its endings:
    `{task_id: ending}` (`TaskEvent.endings`, one query) and `{actor_id: display name}` for every creator and turn author, from ONE `IN` query
    however many tasks - a Project's Live now must not cost a lookup per row. Someone with no
    profile row, or a scrubbed blank name, is absent from the map: the row says null, not a guess."""
    rows = await Task.list_for(
        session,
        actor,
        statuses=statuses,
        project_id=project_id,
        mode=mode,
        pinned=pinned,
        archived=archived,
        q=q,
        limit=limit,
        cursor=cursor,
    )
    people = {t.creator_actor_id for t in rows} | {t.turn_actor_id for t in rows if t.turn_actor_id}
    users = await User.by_actor_ids(session, list(people))
    names = {actor_id: u.name for actor_id, u in users.items() if u.name}
    return rows, names, await TaskEvent.endings(session, rows)


async def replay_events(session: AsyncSession, actor: Actor, task_id: uuid.UUID, since: int = 0):
    """Scope-checked replay of the WHOLE history. `get_for` first - the stream is not a way
    around authorization. Paged under the hood (`REPLAY_PAGE`): a long-form task's record runs
    past any single capped read, and a silently truncated flight recorder is a lie. Served
    through `redact_for`, as the stream is: the private-bearing fields are the
    creator's to read, whichever door the record leaves by."""
    task = await Task.get_for(session, task_id, actor)
    viewer_id, creator_id = actor.id, task.creator_actor_id
    events: list[DurableEvent] = []
    cursor = since
    while True:
        page = await TaskEvent.replay(session, task.id, since=cursor)
        events.extend(redact_for(event, viewer_id=viewer_id, creator_id=creator_id) for event in page)
        if len(page) < REPLAY_PAGE:
            return events
        cursor = page[-1].seq
