# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Gates - the three-action resolve.

A gate is where the machine stops and a human decides. Three actions, verbatim from MCP elicitation
so that a CLI, an HTTP client and an MCP host all mean the same thing by them:

* **accept** - here are the answers. Validated against the QuestionSet, or 422 naming the question.
* **decline** - proceed without me, using the defaults. *Not* a cancellation: the work goes on.
* **cancel** - stop the task.

`operations` owns the transaction and enqueues after commit. The wake-up is a **DBOS
message**, not a poll: the workflow is parked in `DBOS.recv()` and costs nothing while it waits,
which is what makes a 24-hour TTL a sane default rather than a bill.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from typing import Any

import logfire
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.conf import settings
from prismoid.db.base import utcnow
from prismoid.db.models import tenant
from prismoid.db.models.actor import Actor
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.gate import Gate
from prismoid.db.models.steer import Steer
from prismoid.db.models.task import Task
from prismoid.errors import Conflict, Forbidden, NotFound, ValidationFailed
from prismoid.operations import allowance, device_gates, dispatch, gate_answers
from prismoid.operations import inbox as inbox_ops
from prismoid.operations.attribution import actor_ref

# The openers, the budget park and the system's answers live in their own modules; this module
# stays the one name callers import and patch, so each is re-exported here.
from prismoid.operations.gate_budget import BUDGET_TITLE as BUDGET_TITLE
from prismoid.operations.gate_budget import CONFIRM_TITLE as CONFIRM_TITLE
from prismoid.operations.gate_budget import PROJECT_STILL_SPENT as PROJECT_STILL_SPENT
from prismoid.operations.gate_budget import PROJECT_WAVE_TOO_BIG as PROJECT_WAVE_TOO_BIG
from prismoid.operations.gate_budget import advance_status as advance_status
from prismoid.operations.gate_budget import budget_of as budget_of
from prismoid.operations.gate_budget import check_budget as check_budget
from prismoid.operations.gate_budget import confirmation_ahead as confirmation_ahead
from prismoid.operations.gate_budget import consent_to_estimate, open_budget, open_budget_gate, project_over
from prismoid.operations.gate_budget import hold_unstarted as hold_unstarted
from prismoid.operations.gate_budget import person_cap as person_cap
from prismoid.operations.gate_open import APPROVAL_TTL as APPROVAL_TTL
from prismoid.operations.gate_open import CONNECT_TTL as CONNECT_TTL
from prismoid.operations.gate_open import PAUSE_TITLE as PAUSE_TITLE
from prismoid.operations.gate_open import open_approval as open_approval
from prismoid.operations.gate_open import open_connect as open_connect
from prismoid.operations.gate_open import open_control as open_control
from prismoid.operations.gate_open import open_device_away as open_device_away
from prismoid.operations.gate_open import open_pause as open_pause
from prismoid.operations.gate_open import open_question as open_question
from prismoid.operations.gate_open import pause_slot_free as pause_slot_free
from prismoid.operations.gate_sweep import COMPUTER_USE_OFF_BY as COMPUTER_USE_OFF_BY
from prismoid.operations.gate_sweep import CONTROL_ENDED_BY as CONTROL_ENDED_BY
from prismoid.operations.gate_sweep import GATE_TOPIC as GATE_TOPIC
from prismoid.operations.gate_sweep import expire_open as expire_open
from prismoid.operations.gate_sweep import resolve_as_system, resolve_by_timeout, wait_for_the_driver
from prismoid.operations.gate_sweep import resolve_control as resolve_control
from prismoid.operations.gate_sweep import wake_key as wake_key
from prismoid.schemas.device_away import gone_reason
from prismoid.schemas.events import GateResolved
from prismoid.schemas.ids import GateId, ProjectId, TaskId
from prismoid.schemas.plan import (
    WORKSPACE_SCOPE,
    GateAction,
    GateKind,
    GateResolution,
    GateSpec,
    GateState,
    TimeoutPolicy,
)

# Tests reach these by their old private names.
_open_budget = open_budget
_open_budget_gate = open_budget_gate
_resolve_by_timeout = resolve_by_timeout


async def resolve(
    session: AsyncSession,
    actor: Actor,
    gate_id: uuid.UUID,
    action: GateAction,
    content: dict[str, Any] | None = None,
) -> GateResolution:
    """accept | decline | cancel. One transaction; the workflow is woken **after commit**.

    A gate that is already resolved returns its resolution rather than raising. Two people can be
    looking at the same question - and a 409 to the second one is a worse answer than the first
    one's, which is right there.
    """
    gate, task = await _locked_gate(session, actor, gate_id)
    if not gate.is_open():
        return _settled(gate)

    if gate_answers.cancel_stops_task(action, gate.kind, terminal=task.is_terminal()):
        from prismoid.operations import steering  # cycle: steering opens and closes this module's gates

        await steering.cancel_task_row(session, task, actor)
        await session.refresh(gate)
        return _settled(gate)

    spec = GateSpec.model_validate(gate.spec)
    answers = await _answers(session, task, gate, spec=spec, actor=actor, action=action, content=content)
    if action is GateAction.ACCEPT and spec.title == CONFIRM_TITLE:
        await consent_to_estimate(session, task, actor)  # the cap it consented to, not a default nobody chose
    resolution = GateResolution(
        action=action,
        content=answers,
        defaulted=gate_answers.defaulted(spec, content, answers) if action is not GateAction.CANCEL else [],
        resolved_by=actor.display_name,
        resolved_by_id=actor.id,
        at=utcnow(),
    )
    gate.state = GateState.RESOLVED.value
    gate.resolution = resolution.model_dump(mode="json")
    gate.resolved_at = resolution.at
    gate.resolved_by = actor.id
    await inbox_ops.gates_closed(session, task, [gate.id], at=resolution.at)

    seq = await task.next_seq(session)
    await TaskEvent.append(
        session,
        GateResolved(
            seq=seq,
            task_id=TaskId(task.id),
            project_id=ProjectId(task.project_id) if task.project_id else None,
            at=resolution.at,
            actor=actor_ref(actor),
            gate_id=GateId(gate.id),
            action=action,
            content=answers,  # type: ignore[arg-type]
            defaulted=resolution.defaulted,
        ),
    )

    await session.commit()
    await _after_resolve(task, gate, spec, resolution)
    logfire.info("gate {gate} {action}", gate=str(gate.id), action=action.value)
    return resolution


async def _locked_gate(session: AsyncSession, actor: Actor, gate_id: uuid.UUID) -> tuple[Gate, Task]:
    """The gate and its task under the task's row lock (left held), once the actor may answer it.

    **Two people answering at once**: the first commit wins and the second is
    handed it. The first gate read is unlocked, so both could see OPEN and both write; so the
    task's row lock is taken - task before its gates, the order every task writer uses - and the
    gate re-read under it. NO KEY UPDATE: it queues against `next_seq`, never a child insert."""
    gate = await Gate.get_in_workspace(session, gate_id)  # another workspace's gate is absent
    if gate is None:
        raise NotFound("no such gate")

    task = await Task.get_for(session, gate.task_id, actor)  # scope first: a gate is not a side door
    if gate.kind == GateKind.PAUSE.value:
        raise Conflict("a pause lifts when the task resumes")  # the resume door, never here

    await session.refresh(task, with_for_update={"key_share": True})
    await session.refresh(gate)

    # Resolving a gate on a PROJECT task is an editor+ action: a viewer may SEE the
    # question but not answer it. Decided under the lock, like everything else here: a removal
    # or a move that committed while this answer queued is the one that counts. `Task.get_for`
    # is visibility only; a spaceless task is the creator's alone and is unaffected.
    if task.project_id is not None:
        from prismoid.db.models.project import Project, Role

        await Project.get_for(session, task.project_id, actor, min_role=Role.EDITOR)
    return gate, task


async def _answers(
    session: AsyncSession,
    task: Task,
    gate: Gate,
    *,
    spec: GateSpec,
    actor: Actor,
    action: GateAction,
    content: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """What this resolution stores as its answers, after the checks that may refuse it (the
    caller holds the task's row lock). None only where nothing answers."""
    # **A Project ceiling is its owner's.** An accept on a budget gate dispatches the parked wave
    # with no re-check (`run_task._hold_for_budget`), so it would let any editor run one more wave
    # past the owner's cap, per click - whichever ceiling the gate names (the Bursar names the
    # TASK first). So no "Budget reached" accept on a Project task resumes while the Bursar says
    # its Project is over with the gate's wave counted; once the owner raises it, the same accept
    # resumes. The cost confirmation is not a ceiling; decline and cancel spend nothing.
    if action is GateAction.ACCEPT and spec.kind is GateKind.BUDGET and spec.title == BUDGET_TITLE:
        over = await project_over(session, task, spec.budget_figures)
        if over is not None:
            raise Conflict(PROJECT_STILL_SPENT if over.exhausted else PROJECT_WAVE_TOO_BIG)
    # **An allowance comes back, it is not consented to**: accepting an "allowance spent" park would
    # resume work the workspace may not spend on. The wake answers it as the system; a person's accept
    # goes through only once the allowance is back. Cancel always works.
    if (
        action is GateAction.ACCEPT
        and spec.kind is GateKind.BUDGET
        and spec.budget_scope == WORKSPACE_SCOPE
        and await allowance.exhausted(session, task.workspace_id)
    ):
        raise Conflict(allowance.STILL_SPENT)

    answers: dict[str, Any] | None = None
    # **A CONTROL gate is answered by the browser, not by a button**. Only the lease's
    # ending accepts it (`resolve_control`, as the system): a person's accept would resume the
    # agent onto a page nobody handed back. An editor may decline it (Skip) - but not while
    # someone is driving: the hand-back is coming, and it is the answer. **On a device** there is
    # no lease to end: the window is on the creator's own computer, so
    # their accept IS the hand-back - "Done" - and nobody else's is.
    if spec.kind is GateKind.CONTROL:
        answers = await _control_answer(task, gate, actor, action)
    if action is GateAction.ACCEPT and spec.kind is GateKind.APPROVE:
        answers = await _approve_answer(session, task, gate, spec=spec, actor=actor, content=content)

    if action is GateAction.ACCEPT:
        if spec.kind is GateKind.CONNECT:
            # The connect block's accept: attach the resolver's now-live connector
            # to the task HERE, before the wake, so the resumed attempt mounts its tools.
            # Creator-only, like every attach - a connector mounts its owner's credential
            # (the continue_task law); a co-editor's accept still resumes, just tool-less,
            # and the receipt says so honestly.
            return {"connected": await _attach_for_connect(session, task, actor, spec.connector_slug)}
        return gate_answers.accept_answers(spec, content, answers)
    if action is GateAction.DECLINE and spec.kind is not GateKind.CONTROL:
        return gate_answers.decline_answers(spec, content)
    return answers


async def _approve_answer(
    session: AsyncSession,
    task: Task,
    gate: Gate,
    *,
    spec: GateSpec,
    actor: Actor,
    content: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """**Approving a side effect** (the caller holds the task's row lock). The CONNECTOR case is
    creator-only - the approved calls run on the CREATOR's connector
    credential, and a credential never crosses users (the same line `_attach_for_connect` draws
    for the connect gate); it stores no answer of its own. The BROWSER case (the web door)
    is the project's editor+ EXCEPT `always`, which writes a standing allowance in this
    transaction and is creator-only like any other credential-shaped grant."""
    if spec.source != "browser":
        if actor.id != task.creator_actor_id:
            raise Forbidden("only the task's creator can approve actions that run on their connectors")
        return None
    scope = gate_answers.browser_scope(spec.scopes, content)
    if scope == "always":
        if actor.id != task.creator_actor_id:
            raise Forbidden("only the task's creator can always-allow a site")
        from prismoid.operations import egress_allowances as allow_ops
        from prismoid.schemas.computer_use import check_host

        try:
            clean_host = check_host(spec.host)
        except ValueError as bad:
            # Defence in depth: the browser tool only ever offers a host `check_host` would
            # itself accept, but a gate's spec is stored data and this is the one place it
            # turns into a standing grant - never trust it blind.
            raise ValidationFailed(str(bad), fields={"host": str(bad)}) from None

        await allow_ops._insert_allowance(  # pyright: ignore[reportPrivateUsage]
            session, task.creator_actor_id, clean_host, "gate", gate.id
        )
    return {"scope": scope} if scope else {}


async def _after_resolve(task: Task, gate: Gate, spec: GateSpec, resolution: GateResolution) -> None:
    """Wake the parked step and release a consented figure fleet, **after commit**: a
    workflow woken about a row not there yet parks again and waits forever. The wake WEARS its gate
    id: a stale wake applied to a LATER park must not turn an old accept into an approval."""
    await dispatch.get_dispatcher().send(
        wake_key(gate, turn=task.turn),
        {**resolution.model_dump(mode="json"), "gate_id": str(gate.id)},
        topic=GATE_TOPIC,
    )
    if (
        resolution.action is GateAction.ACCEPT
        and spec.kind is GateKind.BUDGET
        and spec.title == CONFIRM_TITLE
    ):
        # Release the held figure fleet: consent was granted with the real numbers in
        # view. Idempotent (deterministic id), after the commit like every enqueue. A decline or
        # cancel never buys a pixel - if the workflow still proceeds, assembly's fallback covers,
        # per the degrade ladder.
        from prismoid.operations import figures as figures_ops

        await figures_ops.enqueue_pregeneration(task.id, task.turn)


async def _control_answer(task: Task, gate: Gate, actor: Actor, action: GateAction) -> dict[str, Any] | None:
    """A person's accept or decline on an open CONTROL gate, as `resolve` stores it (the caller holds
    the task's row lock). Anything else carries no answer."""
    if action is GateAction.ACCEPT:
        return device_gates.device_done(task, gate, actor)
    if action is GateAction.DECLINE:
        from prismoid.operations import machines

        held = await machines.screen.control_state(task.id)
        if held is not None:
            raise Conflict(f"{held.holder_name} is driving the browser.")
        return {"outcome": "declined", "by": actor.display_name}
    return None


def _settled(gate: Gate) -> GateResolution:
    """What a closed gate already says, for the caller who came second. `expire_open` closes a gate
    as a DECLINE with no stored resolution (delivery closed the book, or nobody could answer any
    more): the record's own `gate.resolved{decline}`, said back - never a 500 over a None."""
    if gate.resolution is not None:
        return GateResolution.model_validate(gate.resolution)
    return GateResolution(
        action=GateAction.DECLINE,
        content=None,
        resolved_by="system (expired)",
        at=gate.resolved_at or utcnow(),
    )


async def _attach_for_connect(session: AsyncSession, task: Task, actor: Actor, slug: str) -> bool:
    """The connect gate's accept, made real: the resolver's LIVE connector named by `slug`
    joins the task's attach list. False - honestly - when it cannot: not the creator (a
    credential never crosses users), no such row, not live yet, already attached counts as
    True (idempotent accept), or the task is at its connector cap."""
    from prismoid.db.models.connector import Connector
    from prismoid.operations.connectors import MAX_TASK_CONNECTORS
    from prismoid.schemas.connectors import ATTACHABLE, ConnectorStatus

    if not slug or actor.id != task.creator_actor_id:
        return False
    rows = await Connector.resolve_refs(session, actor.id, [slug])
    row = rows.get(slug)
    if row is None:
        # A catalog-minted row may wear a different slug than its entry ("monday.com" →
        # monday_com vs the gate's catalog slug "monday") - the provenance column is the join.
        owned = await Connector.for_owner(session, actor.id, limit=200)
        row = next((r for r in owned if r.catalog_slug == slug), None)
    if row is None or ConnectorStatus(row.status) not in ATTACHABLE:
        return False
    if str(row.id) in (task.connector_ids or []):
        return True
    if len(task.connector_ids or []) >= MAX_TASK_CONNECTORS:
        return False
    task.connector_ids = [*(task.connector_ids or []), str(row.id)]
    logfire.info("connect gate attached {slug} to task {task}", slug=slug, task=str(task.id))
    return True


async def answers_for(
    session: AsyncSession, task_id: uuid.UUID, resolution: dict[str, Any] | None
) -> dict[str, Any]:
    """The answers, **each one carrying the question it answered.**

    `GateResolution.content` is `{q2: "Board of directors"}` - the MCP-shaped wire form, and correct
    as a wire form. But an answer without its question is a **floating string**, and everything
    downstream is a language model that will attach it to whatever seems plausible.

    That is not hypothetical. The clarifier asked *"Who is the primary audience?"*; the user answered
    *"Board of directors"*; the planner received `q2: Board of directors`, could not know what had
    been asked, and wrote the acceptance criterion **"identifies the board of directors as given in
    the source"** - turning the *audience* into a *content requirement* the brief could not satisfy.
    The step then failed three times trying to find a list of directors in a revenue memo.

    So what is stored - and what lands in `/task/answers.json`, where an agent reads it - is
    self-describing:

        {"q2": {"question": "Who is the primary audience?", "answer": "Board of directors"}}
    """
    content = (resolution or {}).get("content") or {}
    if not content:
        return {}

    asked: dict[str, str] = {}
    for gate in await Gate.for_task(session, task_id):
        spec = GateSpec.model_validate(gate.spec)
        if spec.questions:
            asked |= {q.id: q.prompt for q in spec.questions.questions}

    return {qid: {"question": asked.get(qid, qid), "answer": value} for qid, value in content.items()}


async def open_gates(session: AsyncSession, actor: Actor) -> list[tuple[Gate, str | None]]:
    """Everything waiting on this human, each with its task's `shown_driver`. The Inbox notifies;
    this is the queue itself."""
    found = await Gate.open_for(session, actor)
    tasks = {t.id: t for t in await Task.by_ids(session, {g.task_id for g in found})}
    return [(g, device_gates.shown_driver(tasks[g.task_id])) for g in found]


async def open_gate(session: AsyncSession, actor: Actor, gate_id: uuid.UUID) -> tuple[Gate, Task, str | None]:
    """One open gate, its task and the task's `shown_driver`, to anyone who can see the task - how
    a Project editor reads a teammate's question before answering it (their own queue,
    `open_gates`, holds only their tasks'). Scoped as `resolve` is: another workspace's gate, a
    task the caller cannot see, and a gate already settled are all absent."""
    gate = await Gate.get_in_workspace(session, gate_id)
    if gate is None:
        raise NotFound("no such gate")
    task = await Task.get_for(session, gate.task_id, actor)  # scope first: a gate is not a side door
    if not gate.is_open():
        raise NotFound("no open gate - it was answered or it expired")
    return gate, task, device_gates.shown_driver(task)


async def resolve_open_as_cancel(
    session: AsyncSession, task: Task, actor: Actor, *, attribute: bool = True
) -> list[tuple[str, dict[str, Any]]]:
    """Close every open gate on `task` as CANCEL; return `(wake_key, payload)` per gate.

    The ONE home for cancel-resolution: header Cancel and gate-level
    cancel must age together - two copies of "what resolving a gate means" is how they
    drift. Wakes are RETURNED, not sent: the caller owns the transaction and Rule 3 says
    the send happens after its commit, never before. `attribute=False` is a system cancel that
    borrows `actor`'s authority (a Routine's REPLACE, see `steering.cancel_task_row`): it is not
    their act, so neither the events nor the gate row - which a late answerer is handed - name
    them; the row says `system (cancel)`, the clock's convention.
    """
    opened = await Gate.open_for_task(session, task.id)
    return await _resolve_all(session, task, actor, opened, GateAction.CANCEL, attribute=attribute)


async def resolve_pause(session: AsyncSession, task: Task, actor: Actor) -> list[tuple[str, dict[str, Any]]]:
    """The resume door's half: every open PAUSE gate of `task` lifts as ACCEPT in the
    resumer's name, and the parked steps' wakes are returned - sent after the caller's commit
    (Rule 3), like `resolve_open_as_cancel`'s. The caller holds the task's row lock."""
    paused = [g for g in await Gate.open_for_task(session, task.id) if g.kind == GateKind.PAUSE.value]
    return await _resolve_all(session, task, actor, paused, GateAction.ACCEPT)


async def supersede_approvals(
    session: AsyncSession, task: Task, actor: Actor, steer: Steer
) -> list[tuple[str, dict[str, Any], uuid.UUID | None]]:
    """The steer door's half: a write chosen before the steer must not run on
    an approval given after it. Every open APPROVE gate of `task` closes as DECLINE in the steerer's
    name, naming the steer (`superseded_by`), and each parked step's `(wake_key, payload, step_id)`
    is returned - sent after the caller's commit. A question or a connect block asks
    something the steer does not answer: it stays open. The caller holds the task's row lock."""
    approvals = [g for g in await Gate.open_for_task(session, task.id) if g.kind == GateKind.APPROVE.value]
    content = {"superseded_by": str(steer.command_id)}
    wakes = await _resolve_all(session, task, actor, approvals, GateAction.DECLINE, content=content)
    return [(key, payload, gate.step_id) for gate, (key, payload) in zip(approvals, wakes, strict=True)]


async def _resolve_all(
    session: AsyncSession,
    task: Task,
    actor: Actor,
    opened: Sequence[Gate],
    action: GateAction,
    *,
    attribute: bool = True,
    content: dict[str, Any] | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """Resolve each gate as `action` (with `content`, if any): its row, its `gate.resolved` event,
    and the wake its parked workflow is owed (returned, never sent here)."""
    now = utcnow()
    resolution = GateResolution(
        action=action,
        content=content,
        resolved_by=actor.display_name if attribute else f"system ({action.value})",
        resolved_by_id=actor.id if attribute else None,
        at=now,
    )
    payload = resolution.model_dump(mode="json")
    wakes: list[tuple[str, dict[str, Any]]] = []
    for gate in opened:
        gate.state = GateState.RESOLVED.value
        gate.resolution = payload
        gate.resolved_at = now
        gate.resolved_by = actor.id if attribute else None
        seq = await task.next_seq(session)
        await TaskEvent.append(
            session,
            GateResolved(
                seq=seq,
                task_id=TaskId(task.id),
                project_id=ProjectId(task.project_id) if task.project_id else None,
                at=now,
                actor=actor_ref(actor) if attribute else None,
                gate_id=GateId(gate.id),
                action=action,
                content=content,
            ),
        )
        wakes.append((wake_key(gate, turn=task.turn), {**payload, "gate_id": str(gate.id)}))
    await inbox_ops.gates_closed(session, task, [gate.id for gate in opened], at=now)
    return wakes


async def sweep(session: AsyncSession, now: dt.datetime | None = None) -> list[uuid.UUID]:
    """The TTL sweep - every gate whose `expires_at` has passed, resolved by its **own policy**.

    A gate carries an `on_timeout`, and a gate without one is a task parked until somebody
    notices, which is to say forever, because nobody is watching a task they were not told about.
    The three policies are the three sane answers to *"the human did not come back":*

    * **proceed_with_defaults** - resolve it as a `decline`. The defaults are what the clarifier put
      in the questions precisely so that silence is a safe answer. This is the default, and the right
      one for a clarification.
    * **cancel** - resolve it as a `cancel`. The right one for a gate where guessing is worse than
      stopping - a spend approval, an irreversible action.
    * **wait** - do nothing. The human is the only acceptable answer; the task stays parked. `wait`
      with an expiry is a contradiction the schema permits, so this leaves the gate open and moves
      on rather than pretending a timeout it was told to ignore.

    Each expired gate is resolved by `resolve_by_timeout`, which takes `resolve`'s own guard (the
    task's row lock, a re-read of the gate, a gate already closed left alone - the clock never
    overwrites a person) and writes the same `gate.resolved` record and the same wake message, so
    the parked workflow cannot tell a timeout from a human's answer. It is not a second meaning of
    resolving a gate; it is the machine typing the answer the policy chose, without an actor.

    **The kill switch's gate arm**: while `PRISMOID_COMPUTER_USE_ENABLED=false`, every
    open CONTROL gate and every open browser-sourced APPROVE gate is declined too, as the system
    (`COMPUTER_USE_OFF_BY`) with `{outcome: disabled, reason}` - the content a lease the switch
    ends gives its CONTROL gate (`resolve_control`). The parked step wakes, meets
    `_check_computer_use` before `acquire`, and gives its park back. A connector's approval is not
    the switch's business. An expired gate is the clock's first; the same guard leaves a gate a
    person (or the clock) closed meanwhile alone.

    **The gone-for-good arm**: a step waiting for its device longer than
    `settings.local.away_limit_h` ends its task with what it had (`end_device_gone`).

    **The allowance arm** (`allowance.wake_parks`, ADR-103): every "allowance spent" park whose
    workspace may spend again is accepted as the system. It is the net under a distribution's own
    wake, which is best effort; with no allowance nothing parks, so nothing wakes.
    """
    now = now or utcnow()
    expired = await Gate.expired(session, now)
    # Read with the expired set, before any gate below binds this platform session to a workspace.
    stopped = [] if settings.computer_use.enabled else await Gate.open_for_computer_use(session)
    gone = await Gate.device_away_before(session, now - dt.timedelta(hours=settings.local.away_limit_h))
    swept: list[uuid.UUID] = []

    for gate in expired:
        spec = GateSpec.model_validate(gate.spec)
        if spec.kind is GateKind.CONTROL and await wait_for_the_driver(session, gate):
            continue  # someone is still at the browser: their hand-back answers it, not the clock
        policy = spec.questions.on_timeout if spec.questions else TimeoutPolicy.PROCEED_WITH_DEFAULTS

        if policy is TimeoutPolicy.WAIT:
            continue  # the expiry does not apply; only a human resolves this one

        action = GateAction.CANCEL if policy is TimeoutPolicy.CANCEL else GateAction.DECLINE
        if not await resolve_by_timeout(session, gate, action):
            continue  # a human (or cancel) closed it between the scan and the lock: theirs stands
        swept.append(gate.id)
        logfire.info("gate {gate} timed out → {action}", gate=str(gate.id), action=action.value)

    if stopped:
        from prismoid.operations.machines.screen import OFF

        content: dict[str, Any] = {"outcome": "disabled", "reason": OFF}
        for gate in stopped:
            if gate.id in swept:
                continue  # the clock answered it first
            if await resolve_as_system(
                session, gate, GateAction.DECLINE, content=content, by=COMPUTER_USE_OFF_BY
            ):
                swept.append(gate.id)
                logfire.info("gate {gate} declined: computer use is off", gate=str(gate.id))

    for gate in gone:
        if await end_device_gone(session, gate):
            swept.append(gate.id)
            logfire.info("gate {gate}: the device never came back - its task ended", gate=str(gate.id))

    swept += await allowance.wake_parks(session)
    return swept


async def end_device_gone(session: AsyncSession, gate: Gate) -> bool:
    """A step waited for its device past `away_limit_h`: its task ends
    with what it had, through the give-up path (`execution.fail`, the one a workflow's failure takes):
    the open gates expire on the record, the task fails saying why (`gone_reason`: the device never
    came back, or nobody resumed the step it cut off), and whatever its
    steps published stays the task's. Then the parked step is woken as cancelled, so its workflow
    takes the cancel exit (`close_cancelled_park`: a browser step's recording, the park given back)
    instead of sleeping out its recv. `resolve`'s guard: the task's row lock, the gate re-read under
    it, and a gate a person's resume closed meanwhile left alone. False when nothing ended."""
    from prismoid.operations import execution  # cycle: the step ladder parks on this module's gates

    task = await session.get(Task, gate.task_id)
    if task is None:  # pragma: no cover - FK guarantees it
        return False
    tenant.bind(session, task.workspace_id)
    await session.refresh(task, with_for_update={"key_share": True})
    await session.refresh(gate)
    if not gate.is_open() or task.is_terminal():
        await session.commit()  # nothing written; release the row lock before the next gate
        return False
    why = gone_reason(GateSpec.model_validate(gate.spec).reason, hours=settings.local.away_limit_h)
    await execution.fail(session, task.id, why)
    await session.commit()
    ended = GateResolution(action=GateAction.CANCEL, resolved_by="system (device gone)", at=utcnow())
    wake = {**ended.model_dump(mode="json"), "gate_id": str(gate.id)}
    await dispatch.get_dispatcher().send(
        wake_key(gate, turn=task.turn), wake, topic=GATE_TOPIC, missing_ok=True
    )
    return True
