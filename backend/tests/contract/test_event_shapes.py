# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Every scenario's turn, held to the shape recorded from the engine as it is today.

The record of a turn (`task_events`: which events, in which order, with the few fields that are
structure) is the contract between the engine and everything that reads it: the web, the CLI, the
verifier, a resumed workflow. A refactor that claims to change no behaviour must leave it alone,
and these tests are how it proves that. They do not read model text, ids, times or sizes (see
`shape.py`), so they do not fail on wording or timing.

A failure prints what changed. If the change is meant, `pytest tests/contract --record-shapes`
rewrites the files and the diff of `shapes/` is the review.
"""

from __future__ import annotations

import pytest

from tests.contract.harness import shape
from tests.contract.harness.rig import Scenario, run_turn
from tests.contract.scenarios.all import SCENARIOS

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("contract")]

# Variances a refactor has been allowed, as `"<event type>.<field>"` to the values a turn may carry
# in place of the recorded one. Empty: today's engine is the record. A change that adds a value
# (a new route source) names it here, with the reason, in the same change.
ALLOWED_VARIANCE: dict[str, frozenset[str]] = {}


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
async def test_the_turn_keeps_its_recorded_shape(
    scenario: Scenario,
    monkeypatch: pytest.MonkeyPatch,
    silent_dispatcher: None,
    fake_frames: type,
    record_shapes: bool,
) -> None:
    turn = await run_turn(scenario, monkeypatch)
    seen = shape.shape_of(scenario.name, turn.outcome, turn.events, turn.facts, turn.edges)
    if record_shapes:
        shape.write(seen)
        return
    changed = shape.differences(shape.read(scenario.name), seen, ALLOWED_VARIANCE)
    assert not changed, "the turn's record changed:\n" + "\n".join(changed)
