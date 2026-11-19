# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A connector-only operator step, planned while computer use is on, takes no browser seat and so
asks for no Machine (nothing prewarms one)."""

from __future__ import annotations

from prismoid.ai.rules.plan import DELIVERS_OUTCOME, validate
from prismoid.operations.machines.need import plan_need
from prismoid.schemas.machine_need import MachineNeed
from prismoid.schemas.plan import PlanProposal, ProposedStep

ROSTER = {"operator": DELIVERS_OUTCOME}


def _send_through_connector() -> PlanProposal:
    return PlanProposal(
        steps=[
            ProposedStep(
                label="s1",
                agent="operator",
                title="Post the note",
                goal="Post 'standup at 10' to the team channel through the connected notes service.",
            )
        ]
    )


def test_a_connector_only_step_does_not_take_the_browser_seat() -> None:
    # computer use on: planning_turns passes every browsing agent here, whatever its step does
    steps = validate(_send_through_connector(), roster=ROSTER, inputs=[], browses=["operator"])[0]
    ((_, spec),) = steps
    assert spec.browser is False


def test_a_connector_only_plan_needs_no_computer() -> None:
    steps = validate(_send_through_connector(), roster=ROSTER, inputs=[], browses=["operator"])[0]
    assert plan_need([spec for _, spec in steps]).need is not MachineNeed.COMPUTER
