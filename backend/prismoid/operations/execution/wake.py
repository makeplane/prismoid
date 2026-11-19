# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A park's wake: a gate's resolution mapped into the run's resume, and the races a park settles.

Every wake goes through `hydrate_resume`, the one mapping from a gate's recorded resolution to the
keys the resumed run reads, whether the answer came live or was recovered from the gate row.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Awaitable, Callable, Mapping

import logfire
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.context.thread import safe_name
from prismoid.conf import settings
from prismoid.db.models.gate import Gate
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.steer import Steer
from prismoid.db.models.task import Task
from prismoid.operations import execution as engine
from prismoid.operations import gates, machines, steering
from prismoid.operations.execution import giveback
from prismoid.operations.execution.caps import (
    CONTROL_HANDED_BACK,
    CONTROL_NOBODY,
    CONTROL_TAKEN_OVER,
    DENIED_BY_USER,
)
from prismoid.operations.execution.record import HANDBACKS, WOKE
from prismoid.operations.execution.record import with_record as _with_record
from prismoid.operations.execution.record import woke as _woke
from prismoid.operations.execution.taps import (
    step_event as _step_event,
)
from prismoid.schemas.park import ParkKind, park_of
from prismoid.schemas.plan import (
    GateKind,
    GateSpec,
    StepState,
    TaskStatus,
)
from prismoid.schemas.screen import held_text
from prismoid.schemas.steering import (
    approval_held_text,
)


async def settle_lease_race(task_id: uuid.UUID, *, holder: str, since: str) -> bool:
    """If the lease has already ended when the gate opens (a race), the gate resolves
    at once. The hook read a live lease (held by `holder`, claimed at `since`) and it ended before
    this gate committed, so the ending's own `resolve_control` may have found no gate. Now the gate
    is visible, ask how that lease ended (`machines.screen.ending_of`: the recorded
    `machine.control_ended` of the lease claimed at `since`, or a run-out lease's own clock) and
    answer the gate with exactly that reason and `held_s` - accepted, or declined when the kill
    switch ended it. No wake (`wake=False`): the caller is the parked workflow itself, and
    continues in place. True when THIS settle closed the gate; False when a lease is still held
    (its ending answers the gate, with a wake) or an ending got there first. Idempotent and
    DBOS-free; best-effort: the TTL backstops."""

    if not since:
        return False
    try:
        claimed = dt.datetime.fromisoformat(since)
        ended = await machines.screen.ending_of(task_id, since=claimed)
        if ended is None:
            return False
        reason, held_s = ended
        return await gates.resolve_control(
            task_id,
            holder=holder,
            held_s=held_s,
            reason=reason,
            lease_since=claimed,
            ended_at=None,  # only this lease's own takeover gate qualifies
            wake=False,
        )
    except Exception:
        logfire.exception("task {task}: a lease that ended under its gate was not settled", task=str(task_id))
        return False


async def settle_parked(task_id: uuid.UUID, step: PlanStep, gate: Gate) -> bool:
    """The recovery arm's settle: the step's open gate is a takeover - its lease and
    holder are on the gate's spec and the park's control channel - so try the race settle a death
    may have skipped. False for any other gate, or when its lease is still held."""

    if gate.kind != GateKind.CONTROL.value:
        return False
    spec = GateSpec.model_validate(gate.spec)
    control = (step.resume_state or {}).get("control")
    if spec.source != "takeover" or not spec.lease_since or not isinstance(control, dict):
        return False
    return await settle_lease_race(task_id, holder=str(control.get("holder") or ""), since=spec.lease_since)


async def ended_meanwhile(session: AsyncSession, task: Task, step: PlanStep) -> str | None:
    """A settle answered the step's gate in place, so no wake will arrive - but the task may have
    ended (a cancel) between the settle's commit and here. Take the task's row (the cancel's lock),
    and if it is terminal, leave exactly as the recv path's cancel exit does: the recording, then
    the park given back (`close_cancelled_park`), then "(cancelled)". None to continue; the row
    lock is then held until the caller's commit, so no cancel slips in before the un-park."""
    await session.refresh(task, with_for_update={"key_share": True})
    if not task.is_terminal():
        return None
    task_id, step_id = task.id, step.id
    await session.commit()
    await giveback.close_cancelled_park(session, task_id, step_id)
    return "(cancelled)"


async def resume_settled(
    session: AsyncSession, task: Task, step: PlanStep, gate_id: uuid.UUID
) -> dict[str, object] | str | None:
    """The race settled in place: `settle_lease_race` answered this step's CONTROL
    gate without a wake, so nothing will ever arrive on its `recv`. Map the gate's resolution onto
    the park's channel (the ONE hydration), take the task off `waiting`, and hand the resume back
    for the ladder to continue the same run - the workflow-recv path's ceremony, without the recv.
    A string when the task ended meanwhile (`_ended_meanwhile`: the caller returns it); None when
    the gate row carries no resolution (the caller parks as usual; the recovery arm reads the row
    when its recv times out)."""

    ended = await ended_meanwhile(session, task, step)
    if ended is not None:
        return ended
    gate = await session.get(Gate, gate_id)
    if gate is None:  # pragma: no cover - the park just committed it
        return None
    await session.refresh(gate)  # resolved on the settle's own session: read the row, not the map
    if gate.resolution is None:
        await session.commit()  # release the task's row
        return None
    await hydrate_resume(session, step, gate.resolution)
    # The resume is the run's from here on; the row keeps only the workflow's record.
    resume = {k: v for k, v in (step.resume_state or {}).items() if k not in (HANDBACKS, WOKE)}
    step.resume_state = _with_record(step, None)
    if task.state is TaskStatus.WAITING:
        await engine.advance(session, task_id=task.id, to=TaskStatus.RUNNING)  # back off the gate
    await _step_event(session, task, step, StepState.RUNNING, detail=resumed_detail(resume))
    await session.commit()
    return resume


def resumed_detail(resume: Mapping[str, object]) -> str:
    """The `step.running` anchor's line when a parked step resumes: what actually happened. A
    CONTROL park says whether someone handed the browser back, nobody came, or the kill switch
    answered it - never "the user's answer" for a gate no button can accept. A pause park
    resumes "after a pause"; a budget park says whether the cap was raised or kept."""
    if "budget_answer" in resume:
        if resume["budget_answer"] == "decline":
            return "stopped at the budget: the cap was kept"
        return "resumed past the budget"  # a raise, or an accept at the same cap
    if "pause_resumed" in resume:
        return "resumed after a pause"
    if "control" in resume:
        outcome = resume.get("control_outcome")
        if outcome == "accepted":
            return "resumed: the browser was handed back"
        if outcome == "disabled":
            return "resumed: computer use is off, so nobody took over the browser"
        return "resumed: nobody took over the browser"
    if "approval_action" in resume or "connect_result" in resume:
        return "resumed with the user's decision"
    return "resumed with the user's answer"


def answer_text(prompts: dict[str, str], content: dict[str, object]) -> str:
    """The gate carried a BATCH (one batch per gate, mid-run too): pair every
    answer back with its prompt so the model knows which is which - the tool call returns ONE
    string. A single answer stays bare, exactly what the old shape returned. Shared by the
    live wake path and the hydrate-from-gate recovery, which used to stuff the raw resolution
    dict where this string belongs."""

    def _text(v: object) -> str:
        return ", ".join(str(x) for x in v) if isinstance(v, list) else str(v)

    items = [(prompts.get(qid, qid), _text(v)) for qid, v in sorted(content.items())]
    if not items:
        return ""
    if len(items) == 1:
        return items[0][1]
    return "The user answered your questions:\n" + "\n".join(
        f"{i + 1}. {p}\n   → {a}" for i, (p, a) in enumerate(items)
    )


def control_answer(
    source: str, action: str, content: Mapping[str, object], *, wait_minutes: int, waited: str | None = None
) -> str:
    """The parked browser call's result: the CONTROL gate's resolution, said to the
    model. Accepted (the lease ended - any reason but the kill switch): who drove and for how
    long, the takeover or the handoff wording. Declined: who skipped, that computer use was
    switched off, or that nobody came in the gate's TTL (`waited`: a device's own
    wait, "24h", said instead of the cloud minutes). Pure; the ONE wording, shared by the live wake
    and the hydrate-from-gate recovery."""
    if action == "accept":
        name = str(content.get("holder") or "Someone")
        try:
            held = int(str(content.get("held_s") or 0))
        except ValueError:
            held = 0
        text = CONTROL_HANDED_BACK if source == "handoff" else CONTROL_TAKEN_OVER
        return text.format(name=name, d=held_text(held))
    by = content.get("by")
    if by:
        why = f"{by} skipped"
    elif content.get("outcome") == "disabled":
        why = "computer use is off"
    else:
        why = f"no one came in {waited}" if waited else f"no one came in {wait_minutes} minutes"
    return CONTROL_NOBODY.format(why=why)


async def gate_prompts(session: AsyncSession, step_id: uuid.UUID) -> dict[str, str]:

    gate = await Gate.latest_for_step(session, step_id)
    if gate is None:
        return {}
    spec = GateSpec.model_validate(gate.spec)
    return {q.id: q.prompt for q in (spec.questions.questions if spec.questions else [])}


async def hydrate_resume(session: AsyncSession, step: PlanStep, resolution: dict[str, object]) -> None:
    """Map a gate resolution onto the park's resume channel: the one mapping the recv's wake and the
    crash-recovery arm both use."""
    state = step.resume_state or {}
    channel = next((read for key, read in BETWEEN if key in state), None)
    channel = channel or CHANNELS[park_of(state).kind]
    step.resume_state = await channel(session, step, state, resolution)


Resolution = dict[str, object]


async def _device_channel(
    session: AsyncSession, step: PlanStep, state: Resolution, resolution: Resolution
) -> Resolution:
    """The device channel: the task's resume lifted it (accept), or its cancel or its
    end closed it. What the park held - an owed cut, a run's resume - stays where it was."""
    return {**state, "device_resumed": True, "cancelled": resolution.get("action") == "cancel"}


async def _control_channel(
    session: AsyncSession, step: PlanStep, state: Resolution, resolution: Resolution
) -> Resolution:
    """The control channel: the parked browser call never ran; its result is the gate's
    outcome - who drove and for how long, or why nobody did."""
    action = resolution.get("action")
    meta = state["control"] if isinstance(state["control"], dict) else {}
    content = resolution.get("content")
    return {
        **state,
        "answer": control_answer(
            str(meta.get("source", "")),
            str(action or ""),
            content if isinstance(content, dict) else {},
            wait_minutes=settings.computer_use.control_wait_s // 60,
            waited=held_text(int(str(meta["wait_s"]))) if meta.get("wait_s") else None,
        ),
        # accepted | declined | timeout | disabled: the resume's step line says which
        "control_outcome": str(content.get("outcome") or "") if isinstance(content, dict) else "",
        "cancelled": action == "cancel",
    }


async def _budget_calls_channel(
    session: AsyncSession, step: PlanStep, state: Resolution, resolution: Resolution
) -> Resolution:
    """The mid-run budget channel: accept resumes the same run, decline
    settles the step partial, cancel takes the cancel exit."""
    action = resolution.get("action")
    return {**state, "budget_answer": str(action), "cancelled": action == "cancel"}


async def _budget_gate_channel(
    session: AsyncSession, step: PlanStep, state: Resolution, resolution: Resolution
) -> Resolution:
    """The budget channel: the owner's answer decides whether the next attempt is bought."""
    return {**state, "budget_action": str(resolution.get("action"))}


async def _connect_channel(
    session: AsyncSession, step: PlanStep, state: Resolution, resolution: Resolution
) -> Resolution:
    """The connect channel: `resolve` already attached (or honestly did not) - its receipt rides
    the resolution content, and the resume tells the model the truth."""
    action = resolution.get("action")
    content = resolution.get("content")
    attached = bool(content.get("connected")) if isinstance(content, dict) else False
    return {
        **state,
        "connect_result": (
            ("connected" if attached else "accepted_unattached") if action == "accept" else "declined"
        ),
        "cancelled": action == "cancel",
    }


async def _pause_channel(
    session: AsyncSession, step: PlanStep, state: Resolution, resolution: Resolution
) -> Resolution:
    """The pause channel: the task's resume lifted it (accept, in the resumer's name) or
    its cancel closed it. The resumer's name is theirs to write: made safe."""
    resumer = str(resolution.get("resolved_by") or "")
    return {
        **state,
        "pause_resumed": True,
        "resumed_by": safe_name(resumer) if resumer else "",
        "cancelled": resolution.get("action") == "cancel",
    }


async def _approval_channel(
    session: AsyncSession, step: PlanStep, state: Resolution, resolution: Resolution
) -> Resolution:
    """The approval channel: the gate's one resolution decides every pending call. accept →
    approve; decline (a human's "no", or the TTL sweep's deny-on-timeout) → deny; cancel keeps the
    existing fail path. A steer that closed the approval holds every call instead."""
    action = resolution.get("action")
    content = resolution.get("content")
    superseded = content.get("superseded_by") if isinstance(content, dict) else None
    if superseded:
        # A steer closed the approval: every pending call was chosen
        # before it, so none runs - each is told who steered, and what, to re-issue if it fits.
        row = await Steer.by_command(session, step.task_id, uuid.UUID(str(superseded)))
        if row is None:
            logfire.warning(
                "step {step} approval superseded by a steer {steer} that is not on record",
                step=str(step.id),
                steer=str(superseded),
            )
        (note,) = await steering.notes(session, [row]) if row else (None,)
        return {
            **state,
            "approval_action": "superseded",
            "superseded_text": approval_held_text(note) if note else DENIED_BY_USER,
            "cancelled": False,
        }
    return {
        **state,
        "approval_action": "accept" if action == "accept" else "decline",
        "cancelled": action == "cancel",
    }


async def _answer_channel(
    session: AsyncSession, step: PlanStep, state: Resolution, resolution: Resolution
) -> Resolution:
    """A question's channel: the answers, said to the model against the gate's own prompts."""
    content = resolution.get("content") or {}
    answer = ""
    if isinstance(content, dict) and content:
        answer = answer_text(await gate_prompts(session, step.id), content)
    return {**state, "answer": answer, "cancelled": resolution.get("action") == "cancel"}


Channel = Callable[[AsyncSession, PlanStep, Resolution, Resolution], Awaitable[Resolution]]

# The parks between attempts, by the key their state saved: waiting for the device, and the budget
# gate before the next attempt.
BETWEEN: tuple[tuple[str, Channel], ...] = (
    ("device_away", _device_channel),
    ("budget_gate", _budget_gate_channel),
)
# The resume channel of each park kind. A new park kind is one row here plus its reader.
CHANNELS: dict[ParkKind, Channel] = {
    ParkKind.CONTROL: _control_channel,
    ParkKind.BUDGET: _budget_calls_channel,
    ParkKind.CONNECT: _connect_channel,
    ParkKind.PAUSE: _pause_channel,
    ParkKind.APPROVAL: _approval_channel,
    ParkKind.QUESTION: _answer_channel,
}


async def store_answer(
    session: AsyncSession,
    step_id: uuid.UUID,
    resolution: dict[str, object] | None,
    *,
    handback: int | None = None,
) -> None:
    """The human answered a step's `ask_user`; put the answer where the resume will find it.

    Called by the `run_step` workflow after its `recv` returns, *before* it runs the ladder again.
    The ladder sees `resume_state["answer"]` and continues the paused run instead of starting a new
    one. A `cancel` resolution moves the whole task to failed; a timeout (`resolution is None`, the
    WAIT policy's clock) leaves it parked - there is no safe default for a mid-run question, and
    the ladder's hydrate-from-gate branch then consults the gate ROW, so an answer whose wake was
    lost is recovered rather than replayed as silence. (Stamping `answer: ""` on timeout - the
    first cut - both resumed the run answerless AND disarmed that hydrate net.)

    `handback` is the workflow's count of hand-backs, this park's included: a wake at or before
    the last one stored (`WOKE`) is a replay's recorded recv, already stored, and is left alone.
    """
    if resolution is None:
        return
    step = await session.get(PlanStep, step_id)
    if step is None or not step.resume_state:  # pragma: no cover - the workflow only calls this parked
        return
    if handback is not None and handback <= _woke(step):
        return  # a replay's recorded wake: the door replays on, or recovers the run it resumed
    await hydrate_resume(session, step, resolution)
    if handback is not None:
        step.resume_state = {**(step.resume_state or {}), WOKE: handback}
