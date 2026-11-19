# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Tasks - the domain noun. `POST /v1/tasks` is where a goal becomes work.

Thin: auth → validate → one operation or fat-model call → serialize. ≤20 lines each, no
business logic, no `ai/` import, no transaction. Views come in exactly three shapes -
write-that-starts-machine-work, write-that-wakes-a-job, read - and no others.
"""

from __future__ import annotations

import base64
import binascii
from typing import Annotated
from uuid import UUID

from fastapi import Query

from prismoid.api.deps import ActorDep, CredentialDep, PageDep, SessionDep
from prismoid.api.serializers.common import PageOut
from prismoid.api.serializers.machines import machine_out_or_none
from prismoid.api.serializers.tasks import (
    CostSummaryOut,
    DeletedOut,
    FeedbackIn,
    FeedbackOut,
    FollowUpIn,
    ForkIn,
    MoveIn,
    MoveOut,
    ReplanIn,
    ReplanOut,
    RetryIn,
    StatusOut,
    SteerIn,
    SteerOut,
    TaskAcceptedOut,
    TaskBudgetIn,
    TaskBudgetOut,
    TaskCountsOut,
    TaskCreateIn,
    TaskDetailOut,
    TaskInputIn,
    TaskSummaryOut,
    TaskUpdateIn,
    steer_out,
    task_accepted,
    task_detail,
    task_summary,
)
from prismoid.api.serializers.usage import TaskUsageOut, task_usage_out
from prismoid.db.models.task import Task
from prismoid.errors import ValidationFailed
from prismoid.operations import steering
from prismoid.operations import tasks as ops
from prismoid.operations import usage as usage_ops
from prismoid.schemas.inputs import RawInput
from prismoid.schemas.plan import TaskMode


async def create_task(
    body: TaskCreateIn, actor: ActorDep, session: SessionDep, credential: CredentialDep
) -> TaskAcceptedOut:
    task = await ops.create_task(  # tx + event + enqueue-after-commit
        session,
        actor,
        goal=body.goal,
        project_id=body.project_id,
        mode=TaskMode(body.mode),
        credential=credential.kind,
        inputs=[_decode(i) for i in body.inputs],
        skills=body.skills,
        bots=body.bots,
        direct=body.direct,
        connectors=body.connectors,
        models=body.models,
        client_token=body.client_token,
        **body.dials(),
    )
    return task_accepted(task)


def _decode(item: TaskInputIn) -> RawInput:
    """base64 → bytes, **at the edge**. JSON stops being JSON here, so a malformed body is
    a 422 naming the file - not a stack trace in a worker twenty seconds later."""
    try:
        data = base64.b64decode(item.content, validate=True)
    except (binascii.Error, ValueError):
        raise ValidationFailed(
            f"{item.filename}: content is not valid base64",
            fields={item.filename: "not valid base64"},
        ) from None
    return RawInput(filename=item.filename, data=data, media_type=item.media_type)


async def list_tasks(
    page: PageDep,
    actor: ActorDep,
    session: SessionDep,
    status: Annotated[list[str] | None, Query()] = None,
    project_id: UUID | None = None,
    mode: str | None = None,
    pinned: bool | None = None,
    archived: bool = False,
    q: str | None = None,
) -> PageOut[TaskSummaryOut]:
    # `status` repeats (?status=running&status=waiting): a product group is a set of machine
    # states, and each group paginates as its own list.
    rows, names, endings = await ops.list_tasks(  # keyset page + its people and endings, batched
        session,
        actor,
        statuses=status,
        project_id=project_id,
        mode=mode,
        pinned=pinned,
        archived=archived,
        q=q,
        limit=page.limit,
        cursor=page.cursor,
    )
    next_cursor = rows[-1].cursor() if len(rows) == page.limit else None
    return PageOut(items=[task_summary(t, names, endings) for t in rows], next_cursor=next_cursor)


async def count_tasks(
    actor: ActorDep,
    session: SessionDep,
    project_id: UUID | None = None,
    mode: str | None = None,
    archived: bool = False,
    q: str | None = None,
) -> TaskCountsOut:
    return TaskCountsOut(
        counts=await Task.counts_for(session, actor, project_id=project_id, mode=mode, archived=archived, q=q)
    )


async def get_task(task_id: UUID, actor: ActorDep, session: SessionDep) -> TaskDetailOut:
    read = await ops.read_task(session, actor, task_id)
    return task_detail(
        read.task,
        read.plan,
        CostSummaryOut(total_usd=read.cost.total_usd, calls=read.cost.calls),
        input_urls={str(i["filename"]): ops.input_download_url(i) for i in read.task.inputs},
        can_edit=read.can_edit,
        can_curate=read.can_curate,
        is_creator=read.is_creator,
        creator_name=read.creator_name,
        machine=machine_out_or_none(read.machine),
        can_end_control=read.can_end_control,
        waiting_for=read.waiting_for,
    )


async def cancel_task(task_id: UUID, actor: ActorDep, session: SessionDep) -> StatusOut:
    return StatusOut(status=await steering.cancel(session, actor, task_id))


async def delete_task(task_id: UUID, actor: ActorDep, session: SessionDep) -> DeletedOut:
    await ops.delete_task(session, actor, task_id)
    return DeletedOut(deleted=str(task_id))


async def archive_task(task_id: UUID, actor: ActorDep, session: SessionDep) -> TaskSummaryOut:
    return task_summary(await ops.archive_task(session, actor, task_id))


async def unarchive_task(task_id: UUID, actor: ActorDep, session: SessionDep) -> TaskSummaryOut:
    return task_summary(await ops.unarchive_task(session, actor, task_id))


async def update_task(
    body: TaskUpdateIn, task_id: UUID, actor: ActorDep, session: SessionDep
) -> TaskSummaryOut:
    task = await steering.update_meta(session, actor, task_id, title=body.title, pinned=body.pinned)
    return task_summary(task)


async def move_task(body: MoveIn, task_id: UUID, actor: ActorDep, session: SessionDep) -> MoveOut:
    status = await steering.move(session, actor, task_id, body.project_id)
    return MoveOut(status=status, project_id=str(body.project_id) if body.project_id else None)


async def set_task_budget(
    body: TaskBudgetIn, task_id: UUID, actor: ActorDep, session: SessionDep
) -> TaskBudgetOut:
    """Set a task's USD ceiling - an editor's answer to "Budget reached" (B3). Resuming is the
    gate's own accept, so a client that raises and resumes makes two calls."""
    done = await steering.set_budget(session, actor, task_id, body.usd)
    return TaskBudgetOut(usd=done.usd, previous_usd=done.previous_usd, gate_id=done.gate_id)


async def replan_task(body: ReplanIn, task_id: UUID, actor: ActorDep, session: SessionDep) -> ReplanOut:
    n = await ops.replan(session, actor, task_id, reason=body.directive)  # tx + reenqueue-after-commit
    return ReplanOut(replanned=body.directive, steps=n)


async def steer_task(body: SteerIn, task_id: UUID, actor: ActorDep, session: SessionDep) -> SteerOut:
    row = await steering.steer(  # tx + epoch compare-and-set under the task row lock
        session, actor, task_id, body.directive, command_id=body.command_id, issued_epoch=body.issued_epoch
    )
    return steer_out(row)


async def retract_steer(task_id: UUID, command_id: UUID, actor: ActorDep, session: SessionDep) -> SteerOut:
    return steer_out(await steering.retract(session, actor, task_id, command_id))


async def pause_task(task_id: UUID, actor: ActorDep, session: SessionDep) -> StatusOut:
    return StatusOut(status=await steering.pause(session, actor, task_id))


async def resume_task(task_id: UUID, actor: ActorDep, session: SessionDep) -> StatusOut:
    return StatusOut(status=await steering.resume(session, actor, task_id))


async def follow_up_task(
    body: FollowUpIn, task_id: UUID, actor: ActorDep, session: SessionDep, credential: CredentialDep
) -> TaskAcceptedOut:
    """Continue a settled task on the same row - a new turn, same record, same stream."""
    task = await ops.continue_task(  # tx + event + enqueue-after-commit; SAME task, next turn
        session,
        actor,
        task_id,
        goal=body.goal,
        credential=credential.kind,
        inputs=[_decode(i) for i in body.inputs],
        skills=body.skills,
        bots=body.bots,
        connectors=body.connectors,
        models=body.models,
        client_token=body.client_token,
        **body.dials(),
    )
    return task_accepted(task)


async def retry_task(
    task_id: UUID, actor: ActorDep, session: SessionDep, body: RetryIn | None = None
) -> TaskAcceptedOut:
    """Run the latest ask again as a new turn - same task, whole thread in hand.
    The body is optional: a bare POST retries too (`curl -X POST …/retry`)."""
    task = await ops.retry_task(session, actor, task_id, client_token=body.client_token if body else None)
    return task_accepted(task)


async def feedback_task(body: FeedbackIn, task_id: UUID, actor: ActorDep, session: SessionDep) -> FeedbackOut:
    task = await ops.record_feedback(session, actor, task_id, rating=body.rating)
    return FeedbackOut(rating=body.rating, turn=task.turn)


async def fork_task(body: ForkIn, task_id: UUID, actor: ActorDep, session: SessionDep) -> TaskAcceptedOut:
    child = await ops.fork(session, actor, task_id, client_token=body.client_token, folder=body.folder)
    return task_accepted(child)


async def get_task_usage(task_id: UUID, actor: ActorDep, session: SessionDep) -> TaskUsageOut:
    await Task.get_for(session, task_id, actor)  # scope first: the ledger is not a side door
    return task_usage_out(await usage_ops.for_task(session, task_id))
