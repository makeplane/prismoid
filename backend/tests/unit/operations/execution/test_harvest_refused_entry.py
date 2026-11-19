# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A manifest entry the frame's file door refuses is answered like a missing one: the model is told."""

from __future__ import annotations

import uuid
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models.task import Task
from prismoid.errors import PathRefused, PolicyDenied
from prismoid.operations.execution import harvest as ex_harvest


def _entry(path: str) -> dict[str, Any]:
    return {
        "path": path,
        "type": "document",
        "title": "t",
        "sha256": "0" * 64,
        "bytes": 1,
        "sources": [],
        "agent": "maker",
        "model": "m",
        "skill_pins": [],
        "at": "2026-01-01T00:00:00Z",
    }


class _Frame:
    def __init__(self) -> None:
        self.entries = [_entry("/artifacts/leak.md"), _entry("/artifacts/ok.md")]

    async def manifest(self) -> dict[str, Any]:
        return {"entries": self.entries}

    async def read_bytes(self, path: str) -> bytes:
        if path == "/artifacts/leak.md":  # a link into /browser: sandbar's 403
            raise PathRefused(f"{path}: sandbar refused the path (forbidden)")
        return b"bytes"


async def test_harvest_tells_the_model_a_refused_entry_like_a_missing_one() -> None:
    class _T:
        id = uuid.uuid4()

    class _S:
        id = uuid.uuid4()

    with pytest.raises(PolicyDenied, match=r"leak\.md: the file door refused it"):
        await ex_harvest.harvest(cast(AsyncSession, None), cast(Task, _T()), cast(Any, _S()), _Frame())
