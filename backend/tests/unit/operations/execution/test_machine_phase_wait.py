# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A Machine's phase frames state the bound on the wait they stand for (`wait_s`), so a client can tell
a Machine that is slow to start from a worker that died: the `start` frames carry
`settings.machines.wait_s`, the `end` frames and every other narrator's frames carry none."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, cast

from prismoid.conf import settings
from prismoid.operations import live
from prismoid.operations.execution import acquire
from prismoid.schemas.events import Activity


def _wire(said: list[Activity]) -> live.Wire:
    async def say(event: Any) -> None:
        said.append(event)

    return cast(
        "live.Wire",
        SimpleNamespace(task_id=uuid.uuid4(), step_id=uuid.uuid4(), project_id=None, after_seq=0, say=say),
    )


async def test_machine_phases_carry_the_wait_budget_on_start_only() -> None:
    said: list[Activity] = []
    wire = _wire(said)
    phases = acquire.phases_for(wire, live.activity_emitter(wire), seat=False)

    await phases.open()
    await phases.on_phase("Starting the Machine")
    await phases.close()

    assert [(a.phase, a.tool, a.wait_s) for a in said] == [
        ("start", "machine", settings.machines.wait_s),
        ("end", "machine", None),
        ("start", "machine", settings.machines.wait_s),
        ("end", "machine", None),
    ]


async def test_other_narrators_state_no_wait() -> None:
    said: list[Activity] = []
    wire = _wire(said)
    await live.activity_emitter(wire)("start", "Mounting inputs")
    assert said[0].wait_s is None
