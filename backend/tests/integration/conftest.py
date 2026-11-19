# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Fixtures every integration test may ask for. A file that still defines its own `db`, `actor` or
`client` keeps it (a local fixture wins); new files use these."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.api.app import create_app
from prismoid.api.hub import hub
from prismoid.conf import settings
from prismoid.db.models.actor import Actor
from prismoid.db.session import session_scope
from prismoid.operations import auth
from tests.support.cast import member

# A live stream re-proves its credential this often in tests: short, so a revoked credential's
# stream ends in well under a second instead of the production interval.
_STREAM_REAUTH_S = 0.5


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    """An unbound session (a door binds what it needs), and a drained outbox after: a send still
    pending when the test's loop closes is a destroyed-task warning."""
    async with session_scope() as session:
        yield session
    await auth.flush_outbound_email()


@pytest.fixture
async def actor(db: AsyncSession) -> Actor:
    """The test's person (Ana), committed, in a workspace of her own."""
    row, _ = await member(db, "Ana")
    await db.commit()
    return row


@pytest.fixture
async def client(silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    """The real app over ASGI, with jobs dispatched nowhere and a short stream re-proof interval.
    After: a drained outbox, and the hub's LISTEN stopped before the engine goes (a stream route
    registered on it)."""
    monkeypatch.setattr(settings.api, "stream_reauth_seconds", _STREAM_REAUTH_S)
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as c:
        yield c
    await auth.flush_outbound_email()
    await hub.stop()


@pytest.fixture
def on_the_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Paper off: a serial step takes its Machine up front, for tests that watch the Machine itself (a
    park on paper is test_machine_on_first_tool's)."""
    monkeypatch.setattr(settings.machines, "paper", False)
