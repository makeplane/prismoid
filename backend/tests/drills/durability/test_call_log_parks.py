# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A park resumes through the call log (ADR-089), drill D4 on Prismoid's real step: a writer's step on
paper through the real `execute_step`, each pass its own process (`tests/drills/durability/rig.py`), with
only the model scripted. The step asks after its second read and parks; the body re-entered before the
answer re-issues the park; the answer resumes the same run, which replays what it bought; a SIGKILL
after the resume, and the next pass, finish the step. The provider's ledger is a file, so what was
bought survives the kills."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, cast

import pytest

from prismoid.ai.capabilities.durability import CallLogScope
from prismoid.db.models.actor import Actor
from prismoid.db.models.gate import Gate
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope
from prismoid.operations import execution, gates
from prismoid.operations.call_log import PgCallLog
from prismoid.operations.execution.run import gate_key
from prismoid.schemas.ids import StepId
from prismoid.schemas.plan import GateAction, GateSpec
from tests.support.dispatchers import RecordingDispatcher
from tests.support.rigs.durability_drill import bought, rig
from tests.support.rigs.machine_execution_rig import task_with_steps

pytestmark = [pytest.mark.integration, pytest.mark.drill, pytest.mark.xdist_group("call-log-drills")]

ASKED_AT = 3  # the request whose answer is the question
REQUESTS = 6  # four reads, the question and the memo


async def _gate(task_id: uuid.UUID, step_id: uuid.UUID) -> tuple[Gate, int]:
    """The step's newest gate, and how many it has opened."""
    async with row_scope(Task, task_id) as session:
        newest = await Gate.latest_for_step(session, step_id)
        opened = await Gate.count_for_step(session, step_id)
        assert newest is not None
        session.expunge_all()
        return newest, opened


async def _step(task_id: uuid.UUID, step_id: uuid.UUID) -> PlanStep:
    async with row_scope(Task, task_id) as session:
        step = await session.get(PlanStep, step_id)
        assert step is not None
        session.expunge(step)
        return step


async def _answer(task_id: uuid.UUID, step_id: uuid.UUID, gate: Gate, sent: RecordingDispatcher) -> None:
    """The person answers through the real operation; the workflow stores the wake it was sent."""
    spec = GateSpec.model_validate(gate.spec)
    assert spec.questions is not None
    content = {q.id: "formal" for q in spec.questions.questions}
    async with row_scope(Task, task_id) as session:
        task = await session.get(Task, task_id)
        assert task is not None
        person = await session.get(Actor, task.creator_actor_id)
        assert person is not None
        await gates.resolve(session, person, gate.id, GateAction.ACCEPT, content)
    (wake,) = [cast(dict[str, Any], p) for k, p, _ in sent.sends if k == f"step-{step_id}"]
    async with row_scope(Task, task_id) as session:
        await execution.store_answer(session, step_id, wake, handback=1)
        await session.commit()


async def test_a_park_resumes_its_run_and_buys_nothing_twice(
    tmp_path: Path, recording_dispatcher: RecordingDispatcher
) -> None:
    """Drill D4: park; re-entry before the answer; resume; crash after the resume; resume again."""
    task_id, (step_id,) = await task_with_steps(1, agent="writer")
    ledger = tmp_path / "provider.log"

    code, said = rig("step", task_id, step_id, ledger, "--ask")
    gate, _ = await _gate(task_id, step_id)
    assert code == 0 and said == f"RESULT asking:{gate.id}", said
    assert bought(ledger) == ASKED_AT

    code, said = rig("step", task_id, step_id, ledger, "--ask", "--handed-back", "0")
    assert code == 0 and said == f"RESULT asking:{gate.id}", "the body re-issues the park it took"
    assert bought(ledger) == ASKED_AT, "a re-entry before the answer buys nothing"
    assert (await _gate(task_id, step_id))[1] == 1, "and opens no second gate"

    await _answer(task_id, step_id, gate, recording_dispatcher)
    code, _ = rig("step", task_id, step_id, ledger, "--ask", "--handed-back", "1", "--die-before", "5")
    assert code == -9 and bought(ledger) == ASKED_AT + 1, "the resume replayed the run before it"

    code, said = rig("step", task_id, step_id, ledger, "--ask", "--handed-back", "1")
    step = await _step(task_id, step_id)
    assert code == 0 and step.state == "done", said
    assert bought(ledger) == REQUESTS, "no request was bought twice"
    assert (await _gate(task_id, step_id))[1] == 1, "the question was asked once"
    assert step.attempt == 1, "a park and a crash both stay in their attempt"


async def _logged_answer(task_id: uuid.UUID, step_id: uuid.UUID, gate: Gate) -> object:
    """The answers the step's call log holds under the gate's segment."""
    async with row_scope(Task, task_id) as session:
        task = await session.get(Task, task_id)
        assert task is not None
        workspace = task.workspace_id
    log = PgCallLog(workspace)
    latest = await log.latest(StepId(step_id))
    assert latest.key is not None
    segments = CallLogScope(log, latest.key, dict(latest.entries)).segments()
    (answered,) = [s for s in segments if s.key == gate_key(gate.id)]
    return answered.answers


async def test_an_answer_lost_before_the_run_logged_it_is_answered_again_not_asked_again(
    tmp_path: Path, recording_dispatcher: RecordingDispatcher
) -> None:
    """The wake is stored and committed, then the worker dies before the resumed run logs the answer
    (a Machine waking): the next pass hydrates the answer from the gate row again - one gate, the
    person's answer used, nothing bought twice."""
    task_id, (step_id,) = await task_with_steps(1, agent="writer")
    ledger = tmp_path / "provider.log"

    code, said = rig("step", task_id, step_id, ledger, "--ask")
    gate, _ = await _gate(task_id, step_id)
    assert code == 0 and said == f"RESULT asking:{gate.id}", said

    await _answer(task_id, step_id, gate, recording_dispatcher)
    code, _ = rig("step", task_id, step_id, ledger, "--ask", "--handed-back", "1", "--die-on-resume")
    assert code == -9 and bought(ledger) == ASKED_AT, "died after the wake, before the answer was logged"

    code, said = rig("step", task_id, step_id, ledger, "--ask", "--handed-back", "1")
    step = await _step(task_id, step_id)
    assert code == 0 and step.state == "done", said
    assert (await _gate(task_id, step_id))[1] == 1, "the answered question is never asked again"
    assert "formal" in str(await _logged_answer(task_id, step_id, gate)), (
        "the person's answer is the one used"
    )
    assert bought(ledger) == REQUESTS, "no request was bought twice"
