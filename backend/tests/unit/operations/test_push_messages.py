# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The push a phone gets for an inbox item, and the silent one after a read (ADR-100) - the contract
the mobile app reads: every data value a string, null keys left out, the loud channel for work that
waits on the person, `unread` for the item's workspace, the badge and `unread_total` for every
workspace of the person's, one task's alerts folded together. Pure: an
unsaved row in, a message out."""

from __future__ import annotations

import uuid

import pytest

from prismoid.db.models.inbox import InboxItem, InboxKind
from prismoid.operations.push import BODY_MAX, TITLE_MAX, inbox_message, read_message
from prismoid.schemas.push import PushChannel

WORKSPACE = uuid.uuid4()


def _item(kind: InboxKind, **over: object) -> InboxItem:
    fields: dict[str, object] = {
        "id": uuid.uuid4(),
        "workspace_id": WORKSPACE,
        "actor_id": uuid.uuid4(),
        "kind": kind.value,
        "title": "Delivered: the memo",
        "body": "Here it is.",
        "task_id": None,
        "gate_id": None,
        "project_id": None,
        "payload": {},
    }
    return InboxItem(**{**fields, **over})


@pytest.mark.parametrize(
    ("kind", "channel"),
    [
        (InboxKind.GATE_OPENED, PushChannel.NEEDS_YOU),
        (InboxKind.QUESTION, PushChannel.NEEDS_YOU),
        (InboxKind.BUDGET, PushChannel.NEEDS_YOU),
        (InboxKind.TASK_COMPLETED, PushChannel.UPDATES),
        (InboxKind.ESCALATION, PushChannel.UPDATES),
        (InboxKind.ROUTINE_RUN, PushChannel.UPDATES),
        (InboxKind.CONNECTOR_BROKEN, PushChannel.UPDATES),
        (InboxKind.PROJECT_SHARED, PushChannel.UPDATES),
    ],
)
def test_work_waiting_on_the_person_rides_the_loud_channel(kind: InboxKind, channel: PushChannel) -> None:
    assert inbox_message(_item(kind), unread=1, unread_total=1).channel is channel


def test_a_task_item_carries_every_pointer_as_a_string_and_folds_by_task() -> None:
    task, gate, project = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    item = _item(InboxKind.QUESTION, task_id=task, gate_id=gate, project_id=project)
    message = inbox_message(item, unread=4, unread_total=9)

    assert message.data == {
        "type": "inbox",
        "inbox_id": str(item.id),
        "kind": "question",
        "workspace_id": str(WORKSPACE),
        "task_id": str(task),
        "gate_id": str(gate),
        "project_id": str(project),
        "unread": "4",
        "unread_total": "9",
    }
    assert message.badge == 9  # the badge is the person's, across every workspace
    assert message.tag == str(task)
    assert message.thread_id == str(task)
    assert not message.silent


def test_null_keys_are_left_out_and_a_taskless_item_threads_by_kind() -> None:
    item = _item(InboxKind.CONNECTOR_BROKEN, payload={"connector_slug": "linear"})
    message = inbox_message(item, unread=0, unread_total=0)
    assert set(message.data) == {
        "type",
        "inbox_id",
        "kind",
        "workspace_id",
        "connector_slug",
        "unread",
        "unread_total",
    }
    assert message.data["connector_slug"] == "linear"
    assert message.tag is None
    assert message.thread_id == "connector_broken"


def test_a_routine_item_names_its_routine() -> None:
    routine = uuid.uuid4()
    message = inbox_message(
        _item(InboxKind.ROUTINE_PAUSED, payload={"routine_id": str(routine)}), unread=2, unread_total=2
    )
    assert message.data["routine_id"] == str(routine)


def test_the_lock_screen_gets_a_trimmed_title_and_body() -> None:
    message = inbox_message(
        _item(InboxKind.TASK_COMPLETED, title="t" * 400, body="word " * 400), unread=1, unread_total=1
    )
    assert message.title is not None and len(message.title) == TITLE_MAX
    assert message.body is not None and len(message.body) <= BODY_MAX
    assert message.body.endswith("…")


def test_a_read_is_a_silent_push_with_the_new_counts() -> None:
    message = read_message(WORKSPACE, "all", unread=0, unread_total=3)
    assert message.silent
    assert message.title is None and message.body is None
    assert message.data == {
        "type": "inbox_read",
        "inbox_ids": "all",
        "workspace_id": str(WORKSPACE),
        "unread": "0",
        "unread_total": "3",
    }
    assert message.badge == 3
