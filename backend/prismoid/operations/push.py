# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Push - the inbox, on a person's phone (ADR-100).

Every inbox item asks for a push once its transaction commits (`announce`, called by the inbox's
one writer), and every read asks for a silent one the same way, so a phone's badge and list follow
what the person did elsewhere (`announce_read`). Both ride `dispatch.enqueue_after_commit`: dropped
on a rollback, best effort after a commit, never a reason the caller fails. Nothing is asked for
while push is off or the person has no live device. An item's alert asks to wait
`settings.push.alert_delay_s` before any worker sends it (`announce`): a person watching the task
reads the item on the page in that time, and `send_inbox` rings nothing for a read item. The jobs (`jobs/push.py`) call back here: `send_inbox` and `send_read` read the facts, build
the message (`inbox_message`, `read_message`: pure), hand it to the FCM adapter, and delete the
devices whose tokens FCM called dead. A failing token never stops the others, and a
push never fails anything: the inbox row is the record, the push its courtesy.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence

import logfire
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid import fcm
from prismoid.conf import settings
from prismoid.db.models import tenant
from prismoid.db.models.inbox import InboxItem, InboxKind
from prismoid.db.models.push_device import PushDevice
from prismoid.db.session import session_scope
from prismoid.operations import dispatch
from prismoid.schemas.push import PushChannel, PushMessage, SendOutcome

# The kinds whose work waits on the person: they ride the loud channel.
NEEDS_YOU: frozenset[InboxKind] = frozenset({InboxKind.GATE_OPENED, InboxKind.QUESTION, InboxKind.BUDGET})
TITLE_MAX = 120  # a lock screen shows about this much
BODY_MAX = 240
READ_ALL = "all"
READ_IDS_MAX = 64  # a data message holds 4 KB; the counts, always exact, are what clear a phone


def _trim(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def inbox_message(item: InboxItem, *, unread: int, unread_total: int) -> PushMessage:
    """The alert for one inbox item: `unread` is the recipient's count in the item's workspace and
    `unread_total` across all of theirs (the badge), both with this item. Every data value is a
    string, and a key whose value is null is left out."""
    kind = InboxKind(item.kind)
    fields: dict[str, object] = {
        "type": "inbox",
        "inbox_id": item.id,
        "kind": kind.value,
        "workspace_id": item.workspace_id,
        "task_id": item.task_id,
        "gate_id": item.gate_id,
        "project_id": item.project_id,
        "routine_id": item.payload.get("routine_id"),
        "connector_slug": item.payload.get("connector_slug"),
        "unread": unread,
        "unread_total": unread_total,
    }
    task = str(item.task_id) if item.task_id else None
    return PushMessage(
        data={key: str(value) for key, value in fields.items() if value is not None},
        badge=unread_total,
        title=_trim(item.title, TITLE_MAX),
        body=_trim(item.body, BODY_MAX),
        channel=PushChannel.NEEDS_YOU if kind in NEEDS_YOU else PushChannel.UPDATES,
        tag=task,
        thread_id=task or kind.value,
    )


def read_message(workspace_id: uuid.UUID, inbox_ids: str, *, unread: int, unread_total: int) -> PushMessage:
    """The silent push after a read: which items (`all` after a read-all) in which workspace, its new
    count and the person's new total (the badge)."""
    data = {
        "type": "inbox_read",
        "inbox_ids": inbox_ids,
        "workspace_id": str(workspace_id),
        "unread": str(unread),
        "unread_total": str(unread_total),
    }
    return PushMessage(data=data, badge=unread_total, silent=True)


# ── asking (in the caller's transaction) ──────────────────────────────────────────────────────


async def announce(session: AsyncSession, item: InboxItem) -> None:
    """Ask for this item's push once the caller's transaction commits (never if it rolls back), when
    push is on and the recipient has a live device. The job waits out the alert delay before it
    sends, so an item its person read on the task page meanwhile rings nothing."""
    if not fcm.enabled() or not await PushDevice.any_live_for(session, item.actor_id):
        return
    dispatch.enqueue_after_commit(
        session,
        "push_inbox",
        dispatch.push_inbox_job_key(item.id),
        delay_s=settings.push.alert_delay_s,
        item_id=str(item.id),
    )


async def announce_read(
    session: AsyncSession,
    actor_id: uuid.UUID,
    workspace_id: uuid.UUID,
    *,
    item_ids: Sequence[uuid.UUID] | None,
    at: dt.datetime,
) -> None:
    """Ask for the silent push after a read, once the caller's transaction commits: the items read
    (one, a task's on its page, a closed gate's knocks), or all (`item_ids` None, keyed by the
    read's stamp). An item is read once, so any of its ids names the read; the smallest keys it.
    Asked before the commit, like `announce`, so an unreachable dispatcher never turns a committed
    read into an error."""
    if item_ids is not None and not item_ids:
        return
    if not fcm.enabled() or not await PushDevice.any_live_for(session, actor_id):
        return
    if item_ids is None:
        key, ids = dispatch.push_read_all_job_key(actor_id, workspace_id, at), READ_ALL
    else:
        key = dispatch.push_read_job_key(min(item_ids))
        ids = ",".join(str(item_id) for item_id in item_ids[:READ_IDS_MAX])
    dispatch.enqueue_after_commit(
        session, "push_read", key, actor_id=str(actor_id), workspace_id=str(workspace_id), inbox_ids=ids
    )


# ── sending (the jobs' effects; the session is bound to the item's workspace) ─────────────────


async def send_inbox(session: AsyncSession, item_id: uuid.UUID) -> int:
    """Push one inbox item to its recipient's live devices; the count delivered. An item gone by now
    (its task purged, a savepoint rolled it back) pushes nothing, and neither does one read since it
    was written (the person had its task open, or its gate closed): the alert would ring for
    something already seen, and could land after the silent update that cleared it."""
    item = await session.get(InboxItem, item_id)
    if item is None or not item.is_unread():
        return 0
    unread = await InboxItem.unread_count_of(session, item.actor_id)
    message = inbox_message(item, unread=unread, unread_total=await _unread_total(item.actor_id))
    return await _deliver(session, item.actor_id, message)


async def send_read(session: AsyncSession, actor_id: uuid.UUID, inbox_ids: str) -> int:
    """Push the silent read update to a person's live devices, with their unread counts now."""
    unread = await InboxItem.unread_count_of(session, actor_id)
    workspace_id = tenant.workspace_of(session)
    message = read_message(workspace_id, inbox_ids, unread=unread, unread_total=await _unread_total(actor_id))
    return await _deliver(session, actor_id, message)


async def _unread_total(actor_id: uuid.UUID) -> int:
    """The badge: the person's unread count in every workspace, a platform read of its own."""
    async with session_scope(platform=True) as session:  # a person-level count, no one workspace's
        return await InboxItem.unread_total_of(session, actor_id)


async def _deliver(session: AsyncSession, actor_id: uuid.UUID, message: PushMessage) -> int:
    """Send to every live device, prune the dead ones and commit the prune; the count sent."""
    sender = fcm.sender()
    if sender is None:
        return 0
    devices = await PushDevice.live_for(session, actor_id)
    if not devices:
        return 0
    outcomes = await sender.send_all([d.token for d in devices], message)
    dead = _dead(devices, outcomes)
    if dead:
        await PushDevice.forget_ids(session, dead)
        await session.commit()
    sent = outcomes.count(SendOutcome.SENT)
    logfire.info(
        "push {type}: {sent} sent, {dead} pruned, {failed} failed",
        type=message.data["type"],
        sent=sent,
        dead=len(dead),
        failed=outcomes.count(SendOutcome.FAILED),
    )
    return sent


def _dead(devices: Sequence[PushDevice], outcomes: Sequence[SendOutcome]) -> list[uuid.UUID]:
    return [d.id for d, verdict in zip(devices, outcomes, strict=True) if verdict is SendOutcome.DEAD]
