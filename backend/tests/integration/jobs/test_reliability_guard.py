# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The reliability guard, as amended by ADR-019 - the answer is primary.

Three lanes, three pins:
* **standard**: published means DONE, immediately - no in-line verification, the receipt job is
  enqueued after the commit, and the user's answer never waits on a verify frame or a judge.
* **fleet** (unchanged): the in-line weave survives - a seat that published something
  the verifier rejects gets ONE corrective rung, then ships honestly unverified.
* **nothing published**: still the full ladder to a real escalation - there is nothing to be
  honest about, so the guard must not swallow an empty result.

The decision - which way the ladder turns - must not depend on a model's mood, so the
model-and-Docker-bound halves (`_attempt`, `_verify`) are stubbed and the REAL `execute_step`
runs against real rows.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import orjson
import pytest
from sqlalchemy import select

from prismoid import storage
from prismoid.db.base import utcnow
from prismoid.db.models.artifact import Artifact
from prismoid.db.models.plan import MAX_ATTEMPTS, PlanStep
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope, session_scope
from prismoid.errors import PolicyDenied, StepFailed
from prismoid.operations import artifacts as artifact_ops
from prismoid.operations import execution
from prismoid.operations.execution import MAX_VERIFY_ATTEMPTS
from prismoid.operations.execution import finish as ex_finish
from prismoid.schemas.artifacts import Manifest
from prismoid.schemas.contract import Contract
from prismoid.schemas.events import StepStateChanged
from prismoid.schemas.ids import AgentName
from prismoid.schemas.outputs import ArtifactType, OutputRef
from prismoid.schemas.plan import StepKind, StepSpec, StepState
from tests.support.cast import member
from tests.support.dispatchers import RecordingDispatcher

pytestmark = pytest.mark.integration


def _a_spec() -> dict[str, Any]:
    return StepSpec(
        kind=StepKind.STEP,
        agent=AgentName("writer"),
        title="a memo",
        contract=Contract(
            goal="Write a memo on Q3.",
            acceptance=["every figure comes from the brief"],
            inputs=["/task/inputs/brief.md"],
            output=OutputRef(type=ArtifactType.DOCUMENT, title="memo"),
        ),
    ).model_dump(mode="json")


async def _make_task_step(
    state: StepState = StepState.READY, *, mode: str = "standard"
) -> tuple[uuid.UUID, uuid.UUID]:
    async with session_scope() as session:
        actor, _ = await member(session, "Author")
        await session.flush()
        task = Task(
            id=uuid.uuid4(),
            goal="Write a memo on Q3.",
            creator_actor_id=actor.id,
            criteria={},
            inputs=[],
            mode=mode,
        )
        session.add(task)
        await session.flush()
        step = PlanStep(
            id=uuid.uuid4(),
            task_id=task.id,
            ordinal=0,
            kind=StepKind.STEP.value,
            state=state.value,
            title="a memo",
            spec=_a_spec(),
            attempt=0,
        )
        session.add(step)
        await session.commit()
        return task.id, step.id


async def test_a_fleet_seat_that_fails_verification_ships_unverified() -> None:
    """The FLEET lane keeps the weave: an artifact exists, its verdict is FAIL, and
    the seat **ships it** marked unverified rather than failing - after exactly one targeted
    retry (`MAX_VERIFY_ATTEMPTS`). Standard tasks never enter this lane."""
    task_id, step_id = await _make_task_step(mode="fleet")

    async def fake_attempt(
        session: Any,
        task: Any,
        step: Any,
        spec: Any,
        gotchas: Any,
        *_: Any,
        seat: bool = False,
        carry: Any = None,
        cut: Any = None,
        paper: bool = False,
        fresh: bool = True,
    ) -> tuple[str, list[object], None]:
        # An attempt that *published* something - a truthy `produced` list is all the guard reads.
        return "Here is the Q3 memo.", [object()], None

    async def fake_verify(session: Any, produced: Any) -> ex_finish.Receipts:
        # …which verification rejects, every time (a weak judge's spurious FAIL, or a real one).
        findings = [
            "**Q3 memo** was rejected by verification:",
            "- `numbers:grounded` failed: 99.7M is in no source",
        ]
        return ex_finish.Receipts(findings, unchecked=False)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(execution, "_attempt", fake_attempt)
        mp.setattr(ex_finish, "verify", fake_verify)
        async with row_scope(Task, task_id) as session:
            result = await execution.execute_step(session, task_id, step_id)

    # It returned a narration (did not raise), and the narration is honest about being unverified.
    assert not result.startswith(execution.ASKING)
    assert "unverified" in result.lower()

    async with row_scope(Task, task_id) as session:
        step = await session.get(PlanStep, step_id)
        task = await session.get(Task, task_id)
        assert step is not None and task is not None
        assert step.step_state is StepState.DONE  # done, not escalated
        assert step.attempt == MAX_VERIFY_ATTEMPTS  # one produce, one targeted retry, then ship
        assert not task.is_terminal() or task.status != "failed"  # the task was never failed


async def test_a_fleet_seat_no_checker_confirmed_says_so() -> None:
    """A seat whose receipt signed unverified (its judge never answered) ships on its first attempt,
    and its narration, the delivered summary's line for that seat, marks it not verified."""
    task_id, step_id = await _make_task_step(mode="fleet")

    async def fake_attempt(*_: Any, **__: Any) -> tuple[str, list[object], None]:
        return "Here is the Q3 memo.", [object()], None

    async def fake_verify(session: Any, produced: Any) -> ex_finish.Receipts:
        return ex_finish.Receipts([], unchecked=True)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(execution, "_attempt", fake_attempt)
        mp.setattr(ex_finish, "verify", fake_verify)
        async with row_scope(Task, task_id) as session:
            result = await execution.execute_step(session, task_id, step_id)

    assert result.startswith("Here is the Q3 memo.") and ex_finish.UNCHECKED_NOTE in result
    async with row_scope(Task, task_id) as session:
        step = await session.get(PlanStep, step_id)
        assert step is not None and step.step_state is StepState.DONE and step.attempt == 1


async def test_a_step_that_publishes_nothing_still_escalates() -> None:
    """The other half of the fork: there is nothing to be honest *about*, so the ladder climbs to a
    real escalation and the task fails - the guard must not swallow a genuinely empty result."""
    task_id, step_id = await _make_task_step()

    async def fake_attempt(
        session: Any,
        task: Any,
        step: Any,
        spec: Any,
        gotchas: Any,
        *_: Any,
        seat: bool = False,
        carry: Any = None,
        cut: Any = None,
        paper: bool = False,
        fresh: bool = True,
    ) -> tuple[str, list[object]]:
        # Nothing published: the contract required a document and the agent produced none.
        raise PolicyDenied("the contract required a document and nothing was published")

    async def fake_verify(session: Any, produced: Any) -> ex_finish.Receipts:
        return ex_finish.Receipts(
            [], unchecked=False
        )  # never reached with anything to pass; the undelivered finding is prepended upstream

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(execution, "_attempt", fake_attempt)
        mp.setattr(ex_finish, "verify", fake_verify)
        async with row_scope(Task, task_id) as session:
            with pytest.raises(StepFailed):
                await execution.execute_step(session, task_id, step_id)

    async with row_scope(Task, task_id) as session:
        step = await session.get(PlanStep, step_id)
        assert step is not None
        assert step.step_state is StepState.ESCALATED
        assert step.attempt == MAX_ATTEMPTS  # the full ladder, then a diagnosis


async def test_a_standard_step_completes_on_publish_and_enqueues_the_receipt(
    recording_dispatcher: RecordingDispatcher,
) -> None:
    """THE pin: published means DONE - `_verify` is never consulted in the standard
    lane, the step settles the moment the artifact exists, and the receipt job is enqueued
    after the commit under its deterministic id."""
    task_id, step_id = await _make_task_step()
    artifact_id = uuid.uuid4()

    async def fake_attempt(
        session: Any,
        task: Any,
        step: Any,
        spec: Any,
        gotchas: Any,
        *_: Any,
        seat: bool = False,
        carry: Any = None,
        cut: Any = None,
        paper: bool = False,
        fresh: bool = True,
    ) -> tuple[str, list[object], None]:
        return "Here is the Q3 memo.", [SimpleNamespace(id=artifact_id, role="deliverable")], None

    async def exploding_verify(session: Any, produced: Any) -> ex_finish.Receipts:
        raise AssertionError("standard steps must never verify in-line (ADR-019)")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(execution, "_attempt", fake_attempt)
        mp.setattr(ex_finish, "verify", exploding_verify)
        async with row_scope(Task, task_id) as session:
            result = await execution.execute_step(session, task_id, step_id)

    assert result == "Here is the Q3 memo."
    async with row_scope(Task, task_id) as session:
        step = await session.get(PlanStep, step_id)
        assert step is not None
        assert step.step_state is StepState.DONE
        assert step.attempt == 1, "one attempt - no verify rung ever ran"
    assert ("verify_artifact", f"verify-{artifact_id}") in recording_dispatcher.enqueues, (
        "the receipt follows the answer, on its own durable job"
    )


async def test_a_resume_re_anchors_running_on_the_record(
    recording_dispatcher: RecordingDispatcher,
) -> None:
    """The progress tree's timing law: a step resumed after parking on `ask_user` re-emits
    RUNNING with `detail="resumed with the user's answer"` - without it the step's
    running→done delta silently bills the whole parked human interval to the machine."""
    from prismoid.db.models.event import TaskEvent
    from prismoid.schemas.plan import TaskStatus as TS

    task_id, step_id = await _make_task_step(state=StepState.RUNNING)
    async with row_scope(Task, task_id) as session:
        task = await session.get(Task, task_id)
        step = await session.get(PlanStep, step_id)
        assert task is not None and step is not None
        task.transition(TS.PLANNING)
        task.transition(TS.RUNNING)
        task.transition(TS.WAITING)  # parked on the gate, waiting on the human
        step.resume_state = {"answer": {"q1": "eu-west"}, "tool_call_id": "tc1", "history": ""}
        await session.commit()

    async def fake_attempt(
        session: Any, task: Any, step: Any, spec: Any, gotchas: Any, upstream: Any, resume: Any, **_: Any
    ) -> tuple[str, list[object], None]:
        assert resume is not None, "the parked answer must reach the attempt"
        return (
            "Finished with the user's answer.",
            [SimpleNamespace(id=uuid.uuid4(), role="deliverable")],
            None,
        )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(execution, "_attempt", fake_attempt)
        async with row_scope(Task, task_id) as session:
            result = await execution.execute_step(session, task_id, step_id)

    assert result == "Finished with the user's answer."
    async with row_scope(Task, task_id) as session:
        events = await TaskEvent.of_types(session, task_id, ("step.state",))
        resumed = [
            e
            for e in events
            if isinstance(e, StepStateChanged) and e.detail == "resumed with the user's answer"
        ]
        assert len(resumed) == 1, "the un-park re-anchors RUNNING on the record, exactly once"
        assert resumed[0].state is StepState.RUNNING


async def test_harvest_repairs_a_broken_collection_and_notes_it() -> None:
    """A near-miss collection becomes a valid one AT THE DOOR: the stored bytes
    validate, the provenance carries the honest repair notes, and nothing ever refused."""
    from prismoid.operations import artifacts as artifact_ops
    from prismoid.schemas.artifacts import Manifest
    from prismoid.schemas.collection import Collection

    task_id, step_id = await _make_task_step()
    broken = orjson.dumps(
        {
            "collection_version": 2,  # wrong version
            "title": "Launch tracker",
            "fields": [
                {"key": "name", "label": "Name", "type": "text"},
                {"key": "stage", "label": "Stage", "type": "select", "options": ["Planned"]},
            ],
            "rows": [
                {"id": "r1", "cells": {"name": "API", "stage": "Shipped"}},  # stray select value
                {"id": "r1", "cells": {"name": "Web"}},  # duplicate row id
            ],
            "views": [{"name": "Board", "layout": "board", "group_by": "nope"}],  # dangling ref
        }
    )
    manifest = Manifest.model_validate(
        {
            "entries": [
                {
                    "path": "/artifacts/tracker.collection.json",
                    "type": "collection",
                    "title": "Launch tracker",
                    "sha256": storage.sha256(broken),
                    "bytes": len(broken),
                    "sources": [],
                    "agent": "",
                    "model": "",
                    "skill_pins": [],
                    "at": "2026-08-07T00:00:00Z",
                }
            ]
        }
    )
    async with row_scope(Task, task_id) as session:
        [row] = await artifact_ops.harvest(
            session, task_id, step_id, manifest, {"/artifacts/tracker.collection.json": broken}
        )
        await session.commit()
        stored = storage.get(row.sha256)
        repairs = list(row.provenance.get("repairs", []))

    validated = Collection.model_validate(orjson.loads(stored))
    assert repairs, "every repair is noted on the provenance - never a silent rewrite"
    assert row.sha256 == storage.sha256(stored), "the row describes the STORED bytes"
    stage = next(f for f in validated.fields if f.key == "stage")
    assert "Shipped" in stage.options, "the stray value survived as an option"
    assert validated.views[0].group_by == "stage", "the board fell back to a groupable field"


async def test_a_corrupt_publish_is_a_failed_attempt_not_a_dead_thread() -> None:
    """Hash mismatch between the frame's declared digest and the harvested bytes used to raise
    an uncaught NotFound and kill the task; now it is PolicyDenied - a retriable rung."""
    from prismoid.operations import artifacts as artifact_ops
    from prismoid.schemas.artifacts import Manifest

    task_id, step_id = await _make_task_step()
    manifest = Manifest.model_validate(
        {
            "entries": [
                {
                    "path": "/artifacts/memo.md",
                    "type": "document",
                    "title": "memo",
                    "sha256": "0" * 64,  # not the bytes' hash
                    "bytes": 5,
                    "sources": [],
                    "agent": "",
                    "model": "",
                    "skill_pins": [],
                    "at": "2026-08-07T00:00:00Z",
                }
            ]
        }
    )
    async with row_scope(Task, task_id) as session:
        with pytest.raises(PolicyDenied, match="hash mismatch"):
            await artifact_ops.harvest(session, task_id, step_id, manifest, {"/artifacts/memo.md": b"# memo"})


async def test_delivery_closes_every_open_gate_on_the_record() -> None:
    """A still-open gate is moot the moment the answer ships. Delivery expires it AND
    says so - a `gate.resolved` frame seq'd BEFORE `task.delivered`, so no surface can ever
    render a question over a delivered task (four recovery-spawned gates did, live)."""
    import datetime as dt

    from prismoid.db.models.event import TaskEvent
    from prismoid.db.models.gate import Gate
    from prismoid.schemas.plan import GateKind, GateSpec, GateState, Question, QuestionSet, QuestionType
    from prismoid.schemas.plan import TaskStatus as TS

    task_id, _step_id = await _make_task_step(state=StepState.DONE)
    async with row_scope(Task, task_id) as session:
        task = await session.get(Task, task_id)
        assert task is not None
        task.transition(TS.PLANNING)
        task.transition(TS.RUNNING)
        asked = QuestionSet(
            title="One question before I continue",
            questions=[Question(id="q1", prompt="Which region?", type=QuestionType.TEXT)],
            expires_at=utcnow() + dt.timedelta(hours=24),
        )
        session.add(
            Gate(
                task_id=task_id,
                kind=GateKind.INPUT.value,
                state=GateState.OPEN.value,
                spec=GateSpec(kind=GateKind.INPUT, title=asked.title, questions=asked).model_dump(
                    mode="json"
                ),
                expires_at=asked.expires_at,
            )
        )
        await session.flush()

        await execution.deliver(session, task_id, "done", turn=1)
        await session.commit()

        gates = list(await session.scalars(select(Gate).where(Gate.task_id == task_id)))
        assert all(not g.is_open() for g in gates), "delivery leaves no gate open"

        events = await TaskEvent.of_types(session, task_id, ("gate.resolved", "task.delivered"))
        kinds = [e.type for e in events]
        assert kinds == ["gate.resolved", "task.delivered"], (
            "the closure reaches the record BEFORE the delivery - replays fold the dock away"
        )


def _deck(body: bytes) -> Manifest:
    """One published `deck.md`, as an attempt's frame lists it."""
    entry = {
        "path": "/artifacts/deck.md",
        "type": "document",
        "title": "Pitch deck",
        "sha256": storage.sha256(body),
    }
    return Manifest.model_validate(
        {
            "entries": [
                {
                    **entry,
                    "bytes": len(body),
                    "sources": [],
                    "agent": "",
                    "model": "",
                    "skill_pins": [],
                    "at": "2026-08-06T01:00:00Z",
                }
            ]
        }
    )


async def test_a_retried_attempt_revises_its_own_draft() -> None:
    """A step's second attempt republishing its file is the draft's next revision: one lineage on the
    shelf, never a second deck beside the abandoned one."""
    task_id, step_id = await _make_task_step()
    first, second = b"attempt one", b"attempt two, fixed"
    async with row_scope(Task, task_id) as session:
        [draft] = await artifact_ops.harvest(
            session, task_id, step_id, _deck(first), {"/artifacts/deck.md": first}
        )
        [fixed] = await artifact_ops.harvest(
            session, task_id, step_id, _deck(second), {"/artifacts/deck.md": second}
        )
        await session.commit()
        assert fixed.lineage_id == draft.lineage_id and fixed.revision == 2
        assert [a.id for a in await Artifact.latest_for_task(session, task_id)] == [fixed.id]
