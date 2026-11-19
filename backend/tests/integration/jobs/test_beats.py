# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Worker heartbeat - the pulse that lets /healthz say "the machine is off".

A stopped worker must be distinguishable from a busy queue. The beat row is that signal.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.api.app import create_app
from prismoid.conf import settings
from prismoid.db.models.beat import STALE_AFTER_S, WORKER_BEAT, Beat
from prismoid.db.session import session_scope
from prismoid.jobs.heartbeat import pulse_now

pytestmark = pytest.mark.integration


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    async with session_scope() as session:
        yield session


async def test_pulse_writes_a_fresh_beat(db: AsyncSession) -> None:
    await Beat.pulse(db, WORKER_BEAT)
    await db.commit()

    age = await Beat.age_seconds(db, WORKER_BEAT)
    assert age is not None
    assert age < 5.0


async def test_age_is_none_before_any_pulse(db: AsyncSession) -> None:
    # A unique name so a prior test's worker beat does not contaminate.
    age = await Beat.age_seconds(db, "never-beaten-kind")
    assert age is None


async def test_pulse_now_boot_helper() -> None:
    await pulse_now()
    async with session_scope() as session:
        age = await Beat.age_seconds(session, WORKER_BEAT)
    assert age is not None
    assert age < STALE_AFTER_S


async def test_the_beat_names_the_workers_version() -> None:
    """The API stamps its version on every turn and a worker takes only its own, so
    the beat says which version is live - the API's boot compares the two."""
    await pulse_now()
    async with session_scope() as session:
        _age, info = await Beat.read(session, WORKER_BEAT)
    assert info["version"] == settings.worker.app_version


async def test_healthz_bare_discloses_nothing() -> None:
    """Not a bug if this fails - a disclosure. The bare probe is what a stranger can reach; the
    migration revision and worker/queue vitals must never ride it (they live on /healthz/full)."""
    await pulse_now()
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/healthz")
        full_anon = await client.get("/healthz/full")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body.get("checks") is None
    assert full_anon.status_code == 401  # the vitals require an Actor


async def test_healthz_full_reports_worker_ok_after_pulse() -> None:
    await pulse_now()
    import uuid

    from prismoid.operations import auth as auth_ops

    async with session_scope() as session:
        result = await auth_ops.signup(
            session,
            email=f"health{uuid.uuid4().hex[:8]}@example.com",
            name="Health",
            password="a-long-enough-password",
        )
        await session.commit()
        token = result.token

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/healthz/full", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["checks"]["worker"] == "ok"
    assert "database" in body["checks"]
    assert "migration" in body["checks"]
    # The release stamp: /full names the deployed sha; "dev" is the
    # unpinned default this test runs under. Bare /healthz keeps disclosing nothing above.
    assert body["checks"]["version"] == "dev"
