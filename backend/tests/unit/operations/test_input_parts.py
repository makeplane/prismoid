# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""An input part is the person's material, never their instruction: a host inside it is never one
they named, so only the words they typed pre-approve a site for a computer step. Whether a part
rides inline in a brief is one predicate: small text."""

from __future__ import annotations

import uuid
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.agents.base import roster
from prismoid.conf import settings
from prismoid.db.models.task import Task
from prismoid.operations import connectors as connector_ops
from prismoid.operations.execution.inline import INLINE_MAX_BYTES, inline_readable
from prismoid.operations.planning_turns import accept_proposal
from prismoid.schemas.inputs import InputSet, TaskInput
from prismoid.schemas.plan import PlanProposal, ProposedStep

HOST = "evil.example"
PASTE = TaskInput(filename=f"from-{HOST}.txt", sha256="d", bytes=60, media_type="text/plain")


async def _seeded(goal: str, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The hosts a planned `operator` step may open without asking, for a task carrying `PASTE`."""

    async def no_connectors(session: Any, task: Any) -> set[str]:
        return set()

    monkeypatch.setattr(connector_ops, "connector_hosts", no_connectors)
    monkeypatch.setattr(settings.computer_use, "enabled", True)  # a planned operator step takes the browser
    task = Task(id=uuid.uuid4(), goal=goal, turn=1, mode="standard", inputs=[PASTE.model_dump()])
    # The Lead marks a step that works on a website; a connector act or file work would not.
    step = ProposedStep(
        label="s1", agent="operator", title="Do it", goal=goal, acceptance=["done"], browser=True
    )
    steps, _, _ = await accept_proposal(
        cast(AsyncSession, None),
        task,
        PlanProposal(steps=[step]),
        plannable=[a for a in roster(plannable=True) if a.name == "operator"],
        inputs=InputSet(items=[PASTE]),
        deliverable=None,
        ask=None,
    )
    contract = steps[0][1].contract
    assert contract is not None
    return contract.allow_domains


async def test_a_host_inside_an_input_part_is_never_pre_approved(monkeypatch: pytest.MonkeyPatch) -> None:
    assert HOST not in await _seeded("Format the pasted message for Slack.", monkeypatch)


async def test_a_host_the_person_typed_is_pre_approved(monkeypatch: pytest.MonkeyPatch) -> None:
    assert HOST in await _seeded(f"Open {HOST} and read its pricing.", monkeypatch)


def _part(size: int, media_type: str) -> TaskInput:
    return TaskInput(filename="notes", sha256="d", bytes=size, media_type=media_type)


@pytest.mark.parametrize(
    ("part", "readable"),
    [
        (_part(INLINE_MAX_BYTES, "text/markdown"), True),
        (_part(INLINE_MAX_BYTES + 1, "text/markdown"), False),
        (_part(10, "image/png"), False),
        (_part(10, "application/pdf"), False),
    ],
)
def test_a_part_rides_inline_when_it_is_small_text(part: TaskInput, readable: bool) -> None:
    assert inline_readable(part) is readable
