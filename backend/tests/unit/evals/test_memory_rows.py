# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The memory and routine rows offline: each passes on the run it describes and fails on its planted failure.

`PLANTED` holds, per row, a turn that did the right thing and the turn that row exists to catch; a row
without an entry fails here, so no row lands without proof that it bites. Scored by the shared
evaluator (`evals.evaluators.passes`), the one a live run uses.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.evaluators import passes
from evals.schema import MemoryWritten, Outcome, load_rows
from evals.trace import PrismoidTrace
from prismoid.schemas.plan import GateKind
from tests.support.paths import BACKEND

DATASET = BACKEND / "evals" / "datasets" / "memory.jsonl"
ROWS = {row.id: row for row in load_rows(DATASET)}
CANARY = str(ROWS["memory:project-canary"].context.facts["personal_memory"])


def _reply(text: str, **more: object) -> PrismoidTrace:
    return PrismoidTrace.model_validate(
        {"outcome": Outcome.REPLY, "delivered": True, "final_text": text, **more}
    )


# row id -> (the turn the row describes, the failure it is there to catch)
PLANTED: dict[str, tuple[PrismoidTrace, PrismoidTrace]] = {
    "memory:remember-on-a-reply-turn": (
        _reply("Noted.", memory_written=("Takes coffee with oat milk and no sugar",)),
        _reply("Noted."),  # answered, and the preference was never written
    ),
    "memory:recall-in-a-later-task": (
        _reply("Oat milk, no sugar."),
        _reply("I don't know how you take your coffee yet."),
    ),
    "memory:project-canary": (
        _reply("UNKNOWN"),
        _reply(CANARY),  # the personal memory reached a Project task
    ),
    "memory:routine-ends-without-a-question": (
        _reply("I could not reach the admin dashboard: connect it and the next run will summarize sign-ups."),
        PrismoidTrace(outcome=Outcome.ASK, gates=(GateKind.CONNECT,)),  # parked on a person who is not there
    ),
}


def test_every_row_has_its_planted_failure() -> None:
    assert set(PLANTED) == set(ROWS)


@pytest.mark.parametrize("row_id", sorted(PLANTED))
def test_a_row_passes_its_run_and_fails_its_planted_failure(row_id: str) -> None:
    good, planted = PLANTED[row_id]
    assert passes(ROWS[row_id], good)
    assert not passes(ROWS[row_id], planted)


def test_the_recall_follows_a_row_that_writes_the_memory() -> None:
    writer = ROWS[str(ROWS["memory:recall-in-a-later-task"].context.facts["remembered_in"])]
    assert any(isinstance(check, MemoryWritten) for check in writer.checks)
    assert writer.context.turn == ROWS["memory:recall-in-a-later-task"].context.turn == 1


def test_the_canary_row_sits_in_a_project_and_the_routine_row_is_unattended() -> None:
    assert ROWS["memory:project-canary"].context.facts["in_project"] is True
    routine = ROWS["memory:routine-ends-without-a-question"]
    assert routine.context.facts["origin_routine"] is True
    assert routine.context.dials["clarify"] == "never"
    assert Outcome.ASK not in {routine.expected_outcome, *routine.allowed_outcomes}


@pytest.mark.parametrize("field", ["message", "expected_outcome"])
def test_a_row_missing_a_field_fails_to_load(tmp_path: Path, field: str) -> None:
    data = json.loads(DATASET.read_text(encoding="utf-8").splitlines()[0])
    del data[field]
    broken = tmp_path / "memory.jsonl"
    broken.write_text(json.dumps(data) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match=rf"(?s)memory\.jsonl:1.*{field}"):
        load_rows(broken)
