# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What an inbox item and a read ask the job engine for (ADR-100): an item's alert waits the push
setting's delay, so a person watching the task reads the item first and the alert rings nothing; a
read's silent update starts at once; nothing is asked for while push is off. The device check and
the engine are stand-ins: no stack."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid import fcm
from prismoid.conf import settings
from prismoid.db.models.inbox import InboxItem, InboxKind
from prismoid.db.models.push_device import PushDevice
from prismoid.operations import dispatch, push


@pytest.fixture
def asked(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, dict[str, Any]]]:
    """Push on, every person holding a live phone; records what each writer asked to enqueue."""
    calls: list[tuple[str, str, dict[str, Any]]] = []

    async def any_live_for(_session: object, _actor_id: uuid.UUID) -> bool:
        return True

    def enqueue_after_commit(_session: object, job: str, key: str, **kwargs: Any) -> None:
        calls.append((job, key, kwargs))

    monkeypatch.setattr(fcm, "enabled", lambda: True)
    monkeypatch.setattr(PushDevice, "any_live_for", any_live_for)
    monkeypatch.setattr(dispatch, "enqueue_after_commit", enqueue_after_commit)
    return calls


NO_SESSION = cast(AsyncSession, None)  # the stand-ins never touch it


def _item() -> InboxItem:
    return InboxItem(id=uuid.uuid4(), actor_id=uuid.uuid4(), kind=InboxKind.TASK_COMPLETED.value, payload={})


async def test_an_items_alert_waits_the_push_delay(
    asked: list[tuple[str, str, dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.push, "alert_delay_s", 3.0)
    item = _item()

    await push.announce(NO_SESSION, item)

    assert asked == [("push_inbox", f"push-inbox-{item.id}", {"delay_s": 3.0, "item_id": str(item.id)})]


async def test_a_reads_silent_update_starts_at_once(asked: list[tuple[str, str, dict[str, Any]]]) -> None:
    item_id = uuid.uuid4()

    await push.announce_read(
        NO_SESSION, uuid.uuid4(), uuid.uuid4(), item_ids=[item_id], at=dt.datetime.now(dt.UTC)
    )

    ((job, _, kwargs),) = asked
    assert job == "push_read" and "delay_s" not in kwargs


async def test_push_off_asks_for_nothing(
    asked: list[tuple[str, str, dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fcm, "enabled", lambda: False)

    await push.announce(NO_SESSION, _item())

    assert asked == []
