# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Frames emitted DURING a step - their own transactions, and the Lane-2 taps.

Split from operations/execution.py. The reconciliation rule lives
here: every ephemeral stream ends in something durable.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import logfire
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.base import utcnow
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.operations import live
from prismoid.schemas.events import (
    PROMPT_SNAPSHOT_MAX_CHARS,
    ArtifactDelta,
    ArtifactSnapshot,
    StepNarration,
    StepPrompt,
    StepStateChanged,
    ToolCalled,
)
from prismoid.schemas.ids import ProjectId, StepId, TaskId
from prismoid.schemas.lead import LeadEndingKind
from prismoid.schemas.outputs import BlockedBy, OutcomeStatus
from prismoid.schemas.plan import StepState

# A delta is a *preview*, not the document: 3500 bytes is what fits inside NOTIFY's ceiling
# alongside the envelope, and what a reader can take in.


PREVIEW_CHUNK = 3500

# **A snapshot fires the moment one delta can no longer carry the document - and that threshold is
# derived, not chosen.**
#
# Taken literally, "checkpoints every ~8 KB" is wrong for this product: a
# board memo is 3–6 KB, so an 8 KB trigger means the most common artifact in the system **never
# checkpoints at all**, and a reader who reconnects mid-write sees nothing. A first live run showed
# exactly that: deltas streaming, zero snapshots, and the whole reconciliation rule inert.
#
# The purpose says what the number should be. A snapshot exists so that a reconnect renders *the
# latest state* rather than nothing - which matters precisely when the last delta was an incomplete
# picture. So: checkpoint as soon as the document outgrows a single delta, and every delta-width
# after that. Below it, the delta *is* the document, and `artifact.produced` is the durable terminal.
SNAPSHOT_EVERY_BYTES = PREVIEW_CHUNK

# The row is a checkpoint, not the artifact store. The published bytes are content-addressed in S3;
# this is only ever the picture a reconnecting client needs to render.
SNAPSHOT_MAX_BYTES = 60_000

# **`execute_step` returns a narration on success - or this, when the step parked to ask.** A
# `\x00`-prefixed sentinel rather than an exception or a second return channel, because it crosses
# the DBOS step boundary as JSON and the workflow above pattern-matches on it to decide whether to
# ── frames emitted *during* a step get their own transaction ──────────────────────────────────
#
# **Two bugs, one cause, and the cause is that a step's long transaction was doing the recording.**
#
# 1. *`Session is already flushing`.* pydantic-ai executes a turn's tool calls concurrently, and the
#    journal and the preview tap both wrote through the step's `AsyncSession`. A session is not
#    concurrency-safe. It survived M1 and M2 only because a one-source step makes exactly one
#    `read_file` per turn, so there was never a second coroutine; M3.1b gave a step two sources and
#    the race finally had two runners.
#
# 2. *A deadlocked fan-out.* Appending a durable frame calls `next_seq`, which is
#    `UPDATE tasks SET event_seq = event_seq + 1 RETURNING` and **holds a row lock on the task until
#    COMMIT**. Inside a step's transaction, that lock is held for the entire agent run. The other
#    branch of a fan-out blocks on it - both writers sat in `running`, emitted nothing, and
#    `pg_stat_activity` showed the second one waiting on a `transactionid` lock on that exact
#    statement. A "parallel" frontier that runs slower than a serial one.
#
# The fix is the same for both, and it is not a lock - it is the recognition that **the flight
# recorder and the ledger have no business inside the step's transaction at all.** They are
# append-only facts about what has *already happened*. Binding them to the step's outcome also means
# a failed step **rolls back its own record**, destroying the evidence of why it failed (it happened
# once, and only one instance of it was patched).
#
# So each in-flight frame is appended in its own short transaction, on its own session. No shared
# session to race on, no lock held across a model call, and a record that survives the step.


async def record(
    task_id: uuid.UUID, make: Callable[[int], object], workspace_id: uuid.UUID | None = None
) -> int:
    """Append one durable frame, in its own transaction, acting for the task's workspace.
    Returns the seq it was given."""
    from prismoid.db.session import session_scope

    async with session_scope(workspace_id) as session:
        seq = await Task.claim_seq(session, task_id)
        await TaskEvent.append(session, make(seq))  # type: ignore[arg-type]
        await session.commit()
    return seq


def tool_journal(session: AsyncSession, task: Task, step: PlanStep):  # type: ignore[no-untyped-def]
    """The engine's `journal_tool` closure. It emits an *intent*; this turns it into an event with
    a seq - which is persistence's to assign, and the reason `ai/` cannot build the event itself."""

    task_id, step_id = TaskId(task.id), StepId(step.id)
    project_id = ProjectId(task.project_id) if task.project_id else None
    workspace_id = task.workspace_id  # the frame's own session acts for the task's workspace
    # Captured once: the closure is minted per attempt (`_attempt`), and the recording
    # verifier counts one attempt's rows against that attempt's recording.
    attempt = step.attempt

    async def journal(
        tool: str,
        ok: bool,
        summary: str = "",
        duration_ms: int = 0,
        denied_reason: str = "",
        tool_call_id: str = "",
        *,
        ref: str | None = None,
    ) -> None:
        await record(
            task.id,
            lambda seq: ToolCalled(
                seq=seq,
                task_id=task_id,
                step_id=step_id,
                project_id=project_id,
                at=utcnow(),
                tool=tool,
                ok=ok,
                summary=summary,
                duration_ms=duration_ms,
                denied_reason=denied_reason,
                tool_call_id=tool_call_id,
                attempt=attempt,
                ref=ref,
            ),
            workspace_id,
        )

    return journal


async def step_event(
    session: AsyncSession,
    task: Task,
    step: PlanStep,
    state: StepState,
    detail: str = "",
    *,
    outcome: OutcomeStatus | None = None,
    blocked_by: BlockedBy | None = None,
    ending: LeadEndingKind | None = None,
    findings: bool | None = None,
) -> None:
    """A state change on the flight recorder - and, on a retry, **the reason**.

    `detail` is the gotcha: it is what the verifier found, on the
    stream, at the moment the step was sent round again. That is what makes *"why did this take
    three attempts?"* answerable by reading the events rather than by rummaging in a log - and it is
    what M5 promotes into a skill's gotcha section.
    """
    seq = await task.next_seq(session)
    await TaskEvent.append(
        session,
        StepStateChanged(
            seq=seq,
            task_id=TaskId(task.id),
            step_id=StepId(step.id),
            project_id=ProjectId(task.project_id) if task.project_id else None,
            at=utcnow(),
            state=state,
            title=step.title,
            agent=step.spec.get("agent"),
            attempt=step.attempt,
            detail=detail[:2000],
            model_ref=step.spec.get("model_ref"),
            outcome=outcome,
            blocked_by=blocked_by,
            ending=ending,
            findings=findings,
        ),
    )


def preview(session: AsyncSession, task: Task, step: PlanStep, wire: live.Wire):  # type: ignore[no-untyped-def]
    """The Lane-2 tap, and its Lane-1 terminal - **the reconciliation rule, in one closure**.

    Every ephemeral stream must end in something durable, or a client that blinks loses the thing it
    was watching. So a `write_file` on a text artifact does two things:

      * publishes an ephemeral **`artifact.delta`** - the user sees the document appear instead of a
        spinner, and if it is lost in a disconnect, nothing is lost;
      * and, once the document passes ~8 KB, appends a durable **`artifact.snapshot`** - so a
        reconnect mid-write renders the *latest state* rather than nothing.

    That is what makes losing Lane 2 **boring**, and boring is the whole design goal: replay the
    record, render the last snapshot, pick the deltas back up. There is no reconnect code on the
    client, because there is nothing for it to reconstruct.
    """
    written: dict[str, int] = {}
    task_id, step_id = TaskId(task.id), StepId(step.id)
    project_id = ProjectId(task.project_id) if task.project_id else None
    workspace_id = task.workspace_id

    async def preview(path: str, content: str) -> None:
        await wire.send(
            ArtifactDelta(
                task_id=TaskId(task.id),
                step_id=StepId(step.id),
                project_id=ProjectId(task.project_id) if task.project_id else None,
                at=utcnow(),
                after_seq=wire.after_seq,
                channel=f"{step.id}:{path}",
                path=path,
                text=content[:PREVIEW_CHUNK],
            )
        )

        # The durable checkpoint. Measured on the *cumulative* size, because a model does not append
        # to a document - it rewrites the whole thing on every turn - so "how much has been written"
        # is a question about the file's length, not about this call's payload.
        if len(content) <= PREVIEW_CHUNK or len(content) - written.get(path, 0) < SNAPSHOT_EVERY_BYTES:
            return
        written[path] = len(content)

        seq = await record(
            task.id,
            lambda seq: ArtifactSnapshot(
                seq=seq,
                task_id=task_id,
                step_id=step_id,
                project_id=project_id,
                at=utcnow(),
                path=path,
                offset=0,
                content=content[:SNAPSHOT_MAX_BYTES],
            ),
            workspace_id,
        )
        wire.anchor(seq)  # everything ephemeral from here hangs off this

    return preview


async def narrate(session: AsyncSession, task: Task, step: PlanStep, markdown: str) -> None:
    seq = await task.next_seq(session)
    await TaskEvent.append(
        session,
        StepNarration(
            seq=seq,
            task_id=TaskId(task.id),
            step_id=StepId(step.id),
            project_id=ProjectId(task.project_id) if task.project_id else None,
            at=utcnow(),
            markdown=markdown,
        ),
    )


async def record_prompt(
    session: AsyncSession,
    task: Task,
    step: PlanStep,
    agent: str,
    prompt: str,
    *,
    model_ref: str | None = None,
) -> None:
    """One durable `step.prompt` frame per attempt - clipped at the cap with an honest marker.

    Best-effort by contract: the snapshot is a debugging receipt, and a receipt that could fail
    the step it documents would cost more than it records. Any error is logged and swallowed.
    """
    try:
        clipped = len(prompt) > PROMPT_SNAPSHOT_MAX_CHARS
        text = (
            prompt[:PROMPT_SNAPSHOT_MAX_CHARS] + "\n\n[clipped: the prompt continues past the snapshot cap]"
            if clipped
            else prompt
        )
        seq = await task.next_seq(session)
        await TaskEvent.append(
            session,
            StepPrompt(
                seq=seq,
                task_id=TaskId(task.id),
                step_id=StepId(step.id),
                project_id=ProjectId(task.project_id) if task.project_id else None,
                at=utcnow(),
                attempt=step.attempt,
                agent=agent,
                prompt=text,
                truncated=clipped,
                model_ref=model_ref,
            ),
        )
    except Exception:  # pragma: no cover - the receipt must never cost the step
        logfire.exception("step.prompt snapshot failed for step {step_id}", step_id=str(step.id))
