# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A submit's connectors at the door: explicit refs are validated, a fork keeps what it inherited,
and only a fresh, human-born task with nothing attached gets intent attach - capped, with each
mounted id named so its write tools ask first. The two row reads are stubbed; no database."""

from __future__ import annotations

from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models.actor import Actor
from prismoid.operations import connector_attach, task_door
from prismoid.operations import connectors as connector_ops

GOAL = "file the bug in linear"


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []

    async def validate_refs(session: Any, actor: Any, refs: list[str]) -> list[str]:
        seen.append("validate")
        return [f"id-{r}" for r in refs]

    async def auto_attach_candidates(session: Any, actor_id: Any, goal: str) -> tuple[list[str], list[str]]:
        seen.append("auto")
        ids = [f"auto-{n}" for n in range(connector_ops.MAX_TASK_CONNECTORS + 2)]
        return ids, [f"slug{n}:linear" for n in range(len(ids))]

    monkeypatch.setattr(connector_ops, "validate_refs", validate_refs)
    monkeypatch.setattr(connector_attach, "auto_attach_candidates", auto_attach_candidates)
    return seen


class _Actor:
    id = "ada"


async def _door(connectors: list[str], *, inherit: bool = False, routine: bool = False) -> Any:
    return await task_door.create_connectors(
        cast(AsyncSession, None),
        cast(Actor, _Actor()),
        GOAL,
        connectors,
        inherit=inherit,
        routine=routine,
    )


async def test_explicit_refs_are_validated_and_never_auto_attached(calls: list[str]) -> None:
    assert await _door(["linear"]) == (["id-linear"], [], [])
    assert calls == ["validate"]


async def test_a_fork_keeps_its_inherited_ids_untouched(calls: list[str]) -> None:
    assert await _door(["c1", "c2"], inherit=True) == (["c1", "c2"], [], [])
    assert await _door([], inherit=True) == ([], [], [])
    assert calls == []


async def test_a_routine_task_never_auto_attaches(calls: list[str]) -> None:
    assert await _door([], routine=True) == ([], [], [])
    assert calls == []


async def test_a_bare_human_task_auto_attaches_up_to_the_cap(calls: list[str]) -> None:
    ids, pairs, auto_ids = await _door([])
    cap = connector_ops.MAX_TASK_CONNECTORS
    assert ids == [f"auto-{n}" for n in range(cap)]
    assert pairs == [f"slug{n}:linear" for n in range(cap)]
    assert auto_ids == ids and auto_ids is not ids
    assert calls == ["auto"]
