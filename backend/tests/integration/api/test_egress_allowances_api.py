# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Allowed sites, end to end through the API: add vets, list shows live rows, revoke keeps
the row, the list is the owner's alone."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.api.app import create_app
from prismoid.db.models import tenant
from prismoid.db.models.actor import Actor
from prismoid.db.models.egress_allowance import EgressAllowance
from prismoid.db.session import session_scope
from prismoid.operations import egress_allowances as ops

pytestmark = pytest.mark.integration


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    async with session_scope() as session:
        yield session


async def _person(db: AsyncSession, email: str) -> tuple[Actor, str]:
    from prismoid.operations import auth as auth_ops

    result = await auth_ops.signup(db, email=email, name="Egress Test", password="a-long-enough-password")
    actor = await db.get(Actor, result.user.actor_id)
    assert actor is not None
    return actor, result.token


async def test_add_list_revoke_and_isolation(db: AsyncSession) -> None:
    ana, ana_token = await _person(db, "ana@example.test")
    _bo, bo_token = await _person(db, "bo@example.test")
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        h = {"Authorization": f"Bearer {ana_token}"}
        bad = await client.post("/v1/egress-allowances", json={"host": "169.254.169.254"}, headers=h)
        assert bad.status_code == 422 and "host" in bad.json()["error"]["fields"]
        made = await client.post("/v1/egress-allowances", json={"host": "Admin.Example.com."}, headers=h)
        assert (
            made.status_code == 201
            and made.json()["host"] == "admin.example.com"
            and made.json()["source"] == "settings"
        )
        again = await client.post("/v1/egress-allowances", json={"host": "admin.example.com"}, headers=h)
        assert again.status_code == 201 and again.json()["id"] == made.json()["id"], (
            "idempotent on a live row"
        )
        listed = await client.get("/v1/egress-allowances", headers=h)
        assert [r["host"] for r in listed.json()] == ["admin.example.com"]
        other = await client.get("/v1/egress-allowances", headers={"Authorization": f"Bearer {bo_token}"})
        assert other.json() == []
        gone = await client.delete(f"/v1/egress-allowances/{made.json()['id']}", headers=h)
        assert gone.status_code == 200
        assert (await client.get("/v1/egress-allowances", headers=h)).json() == []
        foreign = await client.delete(
            f"/v1/egress-allowances/{made.json()['id']}", headers={"Authorization": f"Bearer {bo_token}"}
        )
        assert foreign.status_code == 404
    assert await ops.hosts_for_owner(db, ana.id) == []


async def test_insert_allowance_races_to_one_winner(db: AsyncSession) -> None:
    """`_insert_allowance` is called from inside ANY caller's own transaction - the settings
    POST (`add_allowance`, committing) and the egress gate's `always` accept (committing as part of
    gate-resolve) are the two real concurrent callers for the same (owner, host). Both can pass
    the `live_host` check before either lands; `ux_egress_allowances_live` lets only one row
    through, and the loser must re-fetch the winner rather than raise an unhandled
    IntegrityError (a 500)."""
    ana, _ = await _person(db, "racer@example.test")
    workspace_id = tenant.workspace_of(db)  # signup bound ana's personal workspace; each racer acts in it

    async def _race() -> EgressAllowance:
        async with session_scope(workspace_id) as session:
            row = await ops._insert_allowance(  # pyright: ignore[reportPrivateUsage]
                session, ana.id, "race.example.com", "settings", None
            )
            await session.commit()
            return row

    winners = await asyncio.gather(*[_race() for _ in range(6)])
    assert len({w.id for w in winners}) == 1, "every racer must land on the same row"

    live = await EgressAllowance.live_for_owner(db, ana.id)
    assert [r.host for r in live] == ["race.example.com"], "exactly one live row, not six"
