# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Opening gates - every kind a task or a step parks on, except the budget's (`gate_budget`).

Each opener writes the gate row and its `gate.opened` event in the caller's transaction, knocks
the inbox where someone must act, and tells a chat-born task's conversation; none commits, and the
caller moves the task's status. `gates.py` re-exports every opener.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.conf import settings
from prismoid.db.base import utcnow
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.gate import Gate
from prismoid.db.models.task import Task
from prismoid.operations import chat as chat_ops
from prismoid.operations import device_gates
from prismoid.operations import inbox as inbox_ops
from prismoid.operations.gate_budget import advance_status
from prismoid.schemas.device_away import DeviceAwayWhy, away_reason, away_title, for_viewer
from prismoid.schemas.events import GateOpened
from prismoid.schemas.ids import GateId, ProjectId, StepId, TaskId
from prismoid.schemas.plan import (
    GateKind,
    GateSpec,
    GateState,
    QuestionSet,
    TaskStatus,
)


async def _tell_conversation(
    session: AsyncSession, task: Task, *, gate_id: uuid.UUID, title: str, body: str
) -> None:
    """A chat-born task's gate lands in its conversation as a SYSTEM row - the
    same transaction as the gate, so the card and the gate are one durable fact."""
    if task.origin_conversation_id is None:
        return
    await chat_ops.on_gate_opened(session, task, gate_id=gate_id, title=title, body=body)


async def open_question(session: AsyncSession, task: Task, step_id: StepId, asked: QuestionSet) -> Gate:
    """A mid-run gate: a step asked the user ONE batch. It carries a `step_id` and leaves the task's status
    to the caller; everything downstream (TTL, resolve, sweep, dock) is the same. Whoever can answer is
    knocked (`QUESTION`), the approval gate's audience: a question nobody sees is a stall."""
    gate = Gate(
        task_id=task.id,
        step_id=step_id,
        kind=GateKind.INPUT.value,
        state=GateState.OPEN.value,
        spec=GateSpec(kind=GateKind.INPUT, title=asked.title, questions=asked).model_dump(mode="json"),
        expires_at=asked.expires_at,
    )
    session.add(gate)
    await session.flush()

    seq = await task.next_seq(session)
    await TaskEvent.append(
        session,
        GateOpened(
            seq=seq,
            task_id=TaskId(task.id),
            project_id=ProjectId(task.project_id) if task.project_id else None,
            at=utcnow(),
            gate_id=GateId(gate.id),
            kind=GateKind.INPUT,
            title=asked.title,
            questions=asked,
            expires_at=asked.expires_at,
        ),
    )
    await inbox_ops.question(session, task, gate_id=gate.id, asked=asked)
    await _tell_conversation(session, task, gate_id=gate.id, title=asked.title, body="")
    return gate


APPROVAL_TTL = dt.timedelta(hours=24)  # like the ask TTL - but expiry DENIES (see below), never waits


async def open_approval(
    session: AsyncSession,
    task: Task,
    step_id: StepId,
    items: list[tuple[str, str, str, str]],  # (tool_call_id, connector name, prefixed tool, preview)
    *,
    source: str = "connector",
    host: str = "",
    scopes: Sequence[str] = (),
    asks_because: Sequence[str] = (),
) -> Gate:
    """A **mid-run approval**: a step wants to run side-effectful connector tools and
    the policy says ask first; `asks_because` is why the approval rules asked (`Rule.why`), which
    the card says first and the inbox knock carries. `GateKind.APPROVE`: `questions=None` (accepting it IS the content,
    `resolve` already knows), the title is
    the headline, and `spec.reason` carries the evidence - one block per call, a
    "{connector}: {tool}" label line then the capped args preview, blocks blank-line separated
    (the dock parses exactly this shape). ONE gate per approval batch (the one-batch law: a
    turn's calls arrive together and resolve together).

    `source` distinguishes a connector consent (the default) from a browser one
    (the web door): a browser ask with `scopes` is an allowlist ask - the
    title asks about the HOST, not the call - and one with no scopes is a plain danger ask,
    which keeps today's "Allow: {preview}?" shape. `resolve` reads `source`/`host`/`scopes`
    off the gate's own spec to decide who may accept and what `always` writes.

    Deny-on-timeout is structural, not code: an approve gate has no questions, so the TTL
    sweep reads its policy as proceed-with-defaults and resolves DECLINE - which the resume
    maps to ToolDenied. A parked approval can therefore never wedge a task past its TTL,
    routine-minted tasks included. The inbox knock rides the same transaction: an approval
    nobody sees is a 24h stall followed by a deny."""
    n = len(items)
    first = items[0]
    scope_list = list(scopes)
    if source == "browser" and scope_list:
        title = f"Allow the browser to open {host}?"
    elif source == "browser":
        title = f"Allow: {first[3]}?" if n == 1 else f"Allow {n} browser actions?"
    else:
        title = f"Allow {first[1]}: {first[2]}?" if n == 1 else f"Allow {n} actions?"
    reason = "\n\n".join(f"{name}: {tool}\n{preview}".rstrip() for _, name, tool, preview in items)
    because = list(dict.fromkeys(asks_because))
    expires = utcnow() + APPROVAL_TTL
    gate = Gate(
        task_id=task.id,
        step_id=step_id,
        kind=GateKind.APPROVE.value,
        state=GateState.OPEN.value,
        spec=GateSpec(
            kind=GateKind.APPROVE,
            title=title,
            questions=None,
            reason=reason,
            source=source,
            host=host,
            scopes=scope_list,
            asks_because=because,
        ).model_dump(mode="json"),
        expires_at=expires,
    )
    session.add(gate)
    await session.flush()

    seq = await task.next_seq(session)
    await TaskEvent.append(
        session,
        GateOpened(
            seq=seq,
            task_id=TaskId(task.id),
            project_id=ProjectId(task.project_id) if task.project_id else None,
            at=utcnow(),
            gate_id=GateId(gate.id),
            kind=GateKind.APPROVE,
            title=title,
            questions=None,
            reason=reason,
            source=source,
            host=host,
            scopes=scope_list,
            asks_because=because,
            expires_at=expires,
        ),
    )
    knock = f"Approval needed: {first[1]}" if n == 1 else f"Approval needed: {n} actions"
    body = f"Asks because {'; '.join(because)}.\n\n{reason}" if because else reason
    # A connector approval runs on the CREATOR's credential and only they may accept it, so only
    # they are knocked; a browser approval is any editor's, so the turn's author hears too.
    await inbox_ops.gate_opened(
        session, task, gate_id=gate.id, title=knock, body=body, creator_only=source != "browser"
    )
    await _tell_conversation(session, task, gate_id=gate.id, title=knock, body=body)
    return gate


CONNECT_TTL = dt.timedelta(hours=24)  # like approvals: expiry DECLINES (continue without), never waits


async def open_connect(
    session: AsyncSession,
    task: Task,
    step_id: StepId,
    *,
    slug: str,
    display: str,
    why: str,
) -> Gate:
    """A **mid-run connect request**: the step needs a service the task does not
    hold, and instead of a dead-end sentence the conversation grows a connect block. The gate
    IS that block: `connector_slug` names what to offer (a catalog entry, or the owner's own
    not-yet-attached row; empty = nothing matched and the block points at the connectors
    page), `reason` carries the agent's one-sentence why, and the title reads as the ask.

    Resolution semantics live in `resolve`: ACCEPT attaches the creator's now-live connector
    to the task before the wake (so the resumed attempt mounts its tools), DECLINE continues
    without, and the TTL declines structurally (questions=None, the approve gate's law). The
    inbox knock rides along - a connect request nobody sees is a 24h stall."""
    title = f"Connect {display} to continue?"
    gate = Gate(
        task_id=task.id,
        step_id=step_id,
        kind=GateKind.CONNECT.value,
        state=GateState.OPEN.value,
        spec=GateSpec(
            kind=GateKind.CONNECT, title=title, questions=None, reason=why, connector_slug=slug
        ).model_dump(mode="json"),
        expires_at=utcnow() + CONNECT_TTL,
    )
    session.add(gate)
    await session.flush()

    seq = await task.next_seq(session)
    await TaskEvent.append(
        session,
        GateOpened(
            seq=seq,
            task_id=TaskId(task.id),
            project_id=ProjectId(task.project_id) if task.project_id else None,
            at=utcnow(),
            gate_id=GateId(gate.id),
            kind=GateKind.CONNECT,
            title=title,
            questions=None,
            reason=why,
            connector_slug=slug,
            expires_at=gate.expires_at,
        ),
    )
    # Creator-only: accepting attaches the creator's own connector (`_attach_for_connect`).
    await inbox_ops.gate_opened(
        session,
        task,
        gate_id=gate.id,
        title=f"Connect {display} to continue",
        body=why,
        creator_only=True,
    )
    await _tell_conversation(session, task, gate_id=gate.id, title=f"Connect {display} to continue", body=why)
    return gate


PAUSE_TITLE = "Paused"


async def pause_slot_free(session: AsyncSession, *, lock: bool = True) -> bool:
    """Is a pause slot free (`settings.steering.max_pause_parks`)? Counted under the deployment's
    pause-slot lock, held to the caller's commit: two steps parking at once queue here, so the
    second counts the first's gate and never takes the last slot twice. `lock=False` is a safe
    point's advisory read: the park itself counts again under the lock."""
    if lock:
        await Gate.lock_pause_slots(session)
    return await Gate.open_pause_count(session) < settings.steering.max_pause_parks


async def open_pause(
    session: AsyncSession, task: Task, step_id: StepId, *, title: str = PAUSE_TITLE, reason: str = ""
) -> Gate:
    """A **mid-step pause**: a paused task's running step parked at its next tool
    dispatch. No questions and no clock (`expires_at` is None, so the sweep never sees it): only
    the task's resume (`resolve_pause`) or its cancel lifts it, and `resolve` refuses it. No inbox
    knock and no conversation line: the pause is a person's own act, already on the record as
    `pause.applied`. The caller leaves the task `paused` - it is not waiting on an answer. `title`
    and `reason` are a step waiting for its device's (`open_device_away`)."""
    gate = Gate(
        task_id=task.id,
        step_id=step_id,
        kind=GateKind.PAUSE.value,
        state=GateState.OPEN.value,
        spec=GateSpec(kind=GateKind.PAUSE, title=title, questions=None, reason=reason).model_dump(
            mode="json"
        ),
        expires_at=None,
    )
    session.add(gate)
    await session.flush()

    seq = await task.next_seq(session)
    await TaskEvent.append(
        session,
        GateOpened(
            seq=seq,
            task_id=TaskId(task.id),
            step_id=step_id,  # the record files the pause under the step it parked, not the plan
            project_id=ProjectId(task.project_id) if task.project_id else None,
            at=utcnow(),
            gate_id=GateId(gate.id),
            kind=GateKind.PAUSE,
            title=title,
            questions=None,
            reason=reason,
            expires_at=None,
        ),
    )
    return gate


async def open_device_away(
    session: AsyncSession,
    task: Task,
    step_id: StepId,
    why: DeviceAwayWhy,
    *,
    device_name: str | None = None,
    head: str | None = None,
    still_running: bool = False,
    exit_code: int | None = None,
) -> Gate:
    """**A step waiting for its device**: a PAUSE gate (`open_pause`) -
    no questions, no clock, only the task's resume (`resolve_pause`) or its end lifts it - wearing
    why in its reason (`device_away:interrupted` - the step was running when Prismoid stopped, and a
    step on the user's real files never re-runs on its own; `device_away:away` - the helper is not
    there) and "Waiting for <device name>" as its title. Unlike a person's pause, nobody asked for
    it: the creator's inbox is knocked (`GATE_OPENED`, the reason's command line left out), and the
    task goes `paused` under its row lock (a person's pause already there stays as it is), so the
    resume door lifts it unchanged. `head` is the command the crash cut off (`still_running`: it
    ran on; `exit_code`: it finished unseen): it rides the reason (`away_reason`), the creator's
    line only."""
    title = away_title(device_name)
    reason = away_reason(why, name=device_name, head=head, still_running=still_running, exit_code=exit_code)
    gate = await open_pause(session, task, step_id, title=title, reason=reason)
    task = await Task.held(session, task.id)
    if task.state is not TaskStatus.PAUSED:
        await advance_status(session, task, TaskStatus.PAUSED)
    body = for_viewer(reason, creator=False).split("\n", 1)[-1]
    await inbox_ops.gate_opened(session, task, gate_id=gate.id, title=title, body=body, creator_only=True)
    return gate


async def open_control(
    session: AsyncSession,
    task: Task,
    step_id: StepId,
    *,
    source: str,
    reason: str,
    holder: str,
    host: str = "",
    lease_since: str = "",
) -> Gate:
    """A **mid-run control gate**: a person is at the step's browser.
    `takeover` - the step's next browser call met a live control lease ("Ada took control of the
    browser"); it knocks no inbox, because the people concerned are already there. `handoff` - the
    agent asked with `browser.handoff` ('The agent needs someone to "sign in to ..."'); it knocks
    the inbox (`GATE_OPENED`) of every editor of the task's Project - anyone who can take control -
    and the creator, and tells a chat-born task's conversation. The agent's `reason` is quoted as
    its own words (a page can influence them), and the body says where the page is in the
    sidecar's words (`host`), so nobody is asked to type a password on the agent's word alone.

    `questions=None`, the approve gate's shape: the TTL (`control_wait_s`, validated under the
    Machines' `park_hold_s`, so a gate nobody answers always times out before the park could sleep
    the Machine; a device's is the away limit, and the park hold leaves it be) declines it
    structurally - once nobody drives (`_wait_for_the_driver`). Only the lease's ending accepts it
    (`resolve_control`); a person's accept is a 409 in `resolve` - except on a device, where no
    lease exists and the creator's accept is "Done" (`device_gates.device_done`). A
    takeover gate keeps `lease_since` - the `since` of the lease that opened it - so only that
    lease's ending answers it. The caller moves the task to `waiting`, as for every mid-run gate."""
    if source != "handoff":
        title = f"{holder} took control of the browser"
    elif reason:
        words = reason.replace('"', "'")  # a quote in the reason never closes the agent's words early
        title = f'The agent needs someone to "{words}"'
    else:
        title = "The agent needs someone at the browser"
    expires = utcnow() + dt.timedelta(seconds=device_gates.control_wait_s(task))
    gate = Gate(
        task_id=task.id,
        step_id=step_id,
        kind=GateKind.CONTROL.value,
        state=GateState.OPEN.value,
        spec=GateSpec(
            kind=GateKind.CONTROL,
            title=title,
            questions=None,
            reason=reason,
            source=source,
            lease_since=lease_since if source == "takeover" else "",
        ).model_dump(mode="json"),
        expires_at=expires,
    )
    session.add(gate)
    await session.flush()

    seq = await task.next_seq(session)
    await TaskEvent.append(
        session,
        GateOpened(
            seq=seq,
            task_id=TaskId(task.id),
            project_id=ProjectId(task.project_id) if task.project_id else None,
            at=utcnow(),
            gate_id=GateId(gate.id),
            kind=GateKind.CONTROL,
            title=title,
            questions=None,
            reason=reason,
            source=source,
            expires_at=expires,
        ),
    )
    if source == "handoff":
        body = f"The browser is on {host}." if host else ""
        # Anyone who can take control may answer it: every editor of the Project hears (UI check D1).
        await inbox_ops.gate_opened(session, task, gate_id=gate.id, title=title, body=body, every_editor=True)
        await _tell_conversation(session, task, gate_id=gate.id, title=title, body=body)
    return gate
