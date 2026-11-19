# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The documents suite's long rows, offline: the ask of an E-03 row runs through the real engine with scripted
models, and the documents scorer (`evals/suites/documents.py`) passes it on what the person got.

A fifteen-page guide is under one run's words, so one writer writes it and the receipt signs it; a thirty-page
guide is past them, so the long-form lane writes it in parts and its QA gate reads the assembled file."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from pydantic_ai.messages import CachePoint, ModelRequest, UserPromptPart

from evals.schema import load_rows
from evals.suites.documents import DATASET, Receipt, case_of, score_live
from evals.trace import PrismoidTrace
from prismoid.ai.agents.run import AgentRunner, Cached, Start
from prismoid.db.models.verification import Verification
from prismoid.db.session import session_scope
from prismoid.schemas.events import ArtifactProduced
from prismoid.schemas.verification import JudgeVerdict, Verdict, VerificationReport
from tests.contract.harness.moves_lanes import judge_seat, receipts
from tests.contract.harness.rig import Scenario, run_turn
from tests.contract.harness.voices import leads, step, submits, writes
from tests.contract.harness.world import World
from tests.contract.scenarios.lanes import CHAPTERS as PARTS
from tests.contract.scenarios.lanes import LONGFORM

pytestmark = pytest.mark.integration

ROW = next(row for row in load_rows(DATASET) if row.id == "documents:long-doc-budgeting-guide")
CHAPTERS = "\n\n".join(f"## Chapter {n}\n\nA real chapter on budgeting." for n in range(1, 9))


async def test_the_fifteen_page_guide_is_one_writer_run_and_its_row_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    passed = submits(JudgeVerdict(verdict=Verdict.PASS, rationale="It is the guide that was asked for."))
    scenario = Scenario(
        "e03_long_doc",
        ROW.message,
        {
            "lead": (leads(step("s1", "writer")),),
            "writer": (writes(CHAPTERS, title="A Beginner's Guide to Budgeting"),),
            "judge": (passed,),
        },
        given=(judge_seat,),
        world=World(price=Decimal("0.0001"), billed=("judge",)),  # a judge counts when metered
        then=(receipts(),),
    )
    turn = await run_turn(scenario, monkeypatch)
    assert turn.outcome == "delivered"
    planned_steps = [e for e in turn.events if e.type == "task.planned"]
    assert planned_steps and all(s.lane_role is None for s in planned_steps[-1].plan.steps), (
        "no long-form lane"
    )
    [made] = [e for e in turn.events if isinstance(e, ArtifactProduced) and e.role == "deliverable"]
    async with session_scope() as session:
        row = await Verification.latest_for(session, uuid.UUID(str(made.artifact_id)))
        assert row is not None
        report = VerificationReport.model_validate(row.report)
    score = score_live(case_of(ROW), PrismoidTrace.from_events(turn.events), Receipt.of_report(report))
    assert score.passed, score.reason


async def test_the_thirty_page_guide_is_written_in_parts_behind_one_cached_head(
    monkeypatch: pytest.MonkeyPatch, silent_dispatcher: None, fake_frames: type
) -> None:
    started: list[Start] = []
    run = AgentRunner.run

    async def heard(runner: AgentRunner, start: Start, **kwargs: Any) -> Any:
        started.append(start)
        return await run(runner, start, **kwargs)

    monkeypatch.setattr(AgentRunner, "run", heard)
    [scenario] = LONGFORM
    turn = await run_turn(scenario, monkeypatch)
    assert turn.outcome == "delivered"
    parts = [s for s in started if isinstance(s, Cached)]
    assert len(parts) == len(PARTS), "every part starts on the cached head"
    assert len({part.head for part in parts}) == 1, "the head is the same bytes for every part"
    tails = [part.tail for part in parts]
    assert len(set(tails)) == len(PARTS)
    assert "No part is written yet" in tails[0]
    assert "## Written so far" in tails[2] and "Why compost covered." in tails[2]
    assert "Building the bin covered." in tails[2]
    opening = parts[0].opening()[0]
    assert isinstance(opening, ModelRequest) and isinstance(opening.parts[0], UserPromptPart)
    assert isinstance(opening.parts[0].content[-1], CachePoint), "the cache point closes the head"
