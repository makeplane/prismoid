# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A deliverable that spent its QA budget still ships, and it still gets its receipt (ADR-019)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from prismoid.operations.execution import finish


async def test_the_receipt_is_enqueued_after_the_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []
    session = MagicMock(commit=AsyncMock(side_effect=lambda: order.append("commit")))
    enqueue = AsyncMock(side_effect=lambda *_: order.append("enqueue"))
    monkeypatch.setattr(finish, "enqueue_verification", enqueue)
    for name in ("_narrate", "_step_event", "promote_dependents"):
        monkeypatch.setattr(finish, name, AsyncMock())
    monkeypatch.setattr(finish, "notes_sentence", lambda _: "Two findings remain.")
    deck = MagicMock()

    note = await finish.accept_with_qa_notes(
        session, MagicMock(), MagicMock(), "The deck is ready.", SimpleNamespace(outcome=None), sign=[deck]
    )

    assert order == ["commit", "enqueue"]
    enqueue.assert_awaited_once_with([deck])
    assert "Two findings remain." in note
