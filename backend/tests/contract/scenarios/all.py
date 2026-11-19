# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The scenarios: one turn each, declared as data. A scenario is the ask, the dials it arrives with,
and what each agent says; `rig.run_turn` runs it against the real engine and `test_event_shapes`
holds its record to `shapes/<name>.json`.

To add one: append a `Scenario` under the heading of its family (here or in `gates.py`, `lanes.py`, `steering.py`), run
`uv run pytest tests/contract --record-shapes -k <name>`, read the file it wrote (does the record
tell the story you meant?), and commit it with the scenario. A scenario never names an event: the
record is what the engine does with the ask.

Agents a scenario does not name answer the same way in every scenario (`rig.MACHINERY`: the namer,
memory_extract, the skill router), so a shape never depends on them. Every new turn is the Lead's
(`lead`) unless a dial's recipe answers it.
"""

from __future__ import annotations

from tests.contract.harness.rig import Scenario
from tests.contract.harness.voices import leads, says, step, writes
from tests.contract.scenarios.gates import GATES
from tests.contract.scenarios.lanes import LANES
from tests.contract.scenarios.steering import STEERING

# ── a turn that is a reply ────────────────────────────────────────────────────────────────────

REPLY = [
    # A trivial ask: the Lead decides the turn and ends it with a reply, the express shape.
    Scenario("reply", "What's the time?", {"lead": (says("It is noon."),)}),
]

# ── a turn that is work ───────────────────────────────────────────────────────────────────────

ESCALATE = [
    # The Lead ends the turn with a plan: its step is done, the plan's step joins the same turn and runs.
    Scenario(
        "lead_plans",
        "Karl Schwarzschild",
        {"lead": (leads(step("s1", "writer")),), "writer": (writes("The memo."),)},
    ),
]

# ── a plan of several waves ───────────────────────────────────────────────────────────────────

PLANS = [
    # Three waves in a chain: each step starts when the one before is done, and says so on the record.
    Scenario(
        "multi_wave_plan",
        "Write me a memo on the Eddington correction",
        {
            "lead": (
                leads(step("s1", "researcher"), step("s2", "writer", "s1"), step("s3", "writer", "s2")),
            ),
            "researcher": (says("The findings, cited."),),
            "writer": (writes("A draft."), writes("The memo.")),
        },
    ),
]

SCENARIOS: list[Scenario] = [*REPLY, *ESCALATE, *PLANS, *LANES, *STEERING, *GATES]
