# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A parked turn resumes as it did the day it was recorded.

Each fixture in `fixtures/` is a turn parked on a gate, as the engine saved it: the rows a recovered
workflow reads. The test puts them back in a fresh workspace, runs the parked step's workflow again
as the boot sweep does after a restart, answers its gate, and holds the record the resume wrote to
the shape recorded beside the rows. A change to how a park is saved that no longer reads a park
already in a database fails here.

`pytest tests/contract --record-shapes -k <name>` parks the scenario again and rewrites the fixture.
"""

from __future__ import annotations

import pytest

from tests.contract.harness import resume, shape
from tests.contract.scenarios.gates import RESUMES

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("contract")]


@pytest.mark.parametrize("case", RESUMES, ids=lambda c: c.name)
async def test_a_parked_turn_resumes_to_its_recorded_shape(
    case: resume.Resume,
    monkeypatch: pytest.MonkeyPatch,
    silent_dispatcher: None,
    fake_frames: type,
    record_shapes: bool,
) -> None:
    if record_shapes:
        parked = await resume.dump(await resume.freeze(case.parked, monkeypatch))
    else:
        parked = resume.read(case.name)["parked"]
    task_id = await resume.restore(parked)
    state, events = await resume.resume(
        case, monkeypatch, task_id, max(e["seq"] for e in parked["task_events"])
    )
    seen = shape.shape_of(case.name, state, events)
    if record_shapes:
        resume.write(case.name, parked, seen)
        return
    changed = shape.differences(resume.read(case.name)["resumed"], seen)
    assert not changed, "the resumed turn's record changed:\n" + "\n".join(changed)
