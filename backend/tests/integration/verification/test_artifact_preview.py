# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The preview sidecar's storage pointer and endpoint contract (ADR-015)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import orjson
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid import storage
from prismoid.db.models.actor import Actor
from prismoid.db.session import session_scope
from prismoid.errors import NotFound
from prismoid.operations import artifacts as ops
from tests.support.cast import member

pytestmark = pytest.mark.integration


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    async with session_scope() as session:
        yield session


@pytest.fixture
async def actor(db: AsyncSession) -> Actor:
    row, _ = await member(db, "Ana")
    await db.commit()
    return row


async def test_preview_serves_the_sidecar_and_404s_absence(db: AsyncSession, actor: Actor) -> None:
    from tests.support.artifacts import a_collection_artifact

    artifact = await a_collection_artifact(db, actor)
    with pytest.raises(NotFound):
        await ops.preview(db, actor, artifact.id)  # NULL pointer = honest absence

    payload = orjson.dumps(
        {"kind": "sheet", "sheets": [{"name": "Model", "dims": "5x2", "rows": [["a", 1]]}]}
    )
    artifact.preview_sha256 = storage.put(payload)
    await db.commit()

    preview = await ops.preview(db, actor, artifact.id)
    assert preview["kind"] == "sheet"
