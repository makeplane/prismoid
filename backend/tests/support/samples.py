# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Sample values a test reads from more than one file: one instance of every event type
(`SAMPLES`), and a legal collection with the mutations that break it (`FIXTURE`, `MUTATIONS`)."""

from __future__ import annotations

import copy
import datetime as dt
from collections.abc import Sequence
from decimal import Decimal
from typing import Any, TypedDict
from uuid import UUID, uuid4

from pydantic import BaseModel

from prismoid.schemas import events as ev
from prismoid.schemas.context import ContextItemView, ContextLaneView
from prismoid.schemas.ids import ActorId, ArtifactId, GateId, MemoryId, ProjectId, StepId, TaskId
from prismoid.schemas.model_class import Family
from prismoid.schemas.outputs import ArtifactType, VerifierSuite
from prismoid.schemas.plan import (
    GateAction,
    GateKind,
    PlanEdit,
    PlanEditKind,
    PlanStepView,
    PlanView,
    Question,
    QuestionSet,
    QuestionType,
    StepKind,
    StepState,
    TaskMode,
    TaskStatus,
)
from prismoid.schemas.qa import QAFinding, QAFindingKind, QASeverity
from prismoid.schemas.steering import SafePoint
from prismoid.schemas.usage import Unit
from prismoid.schemas.verification import Diagnosis, Verdict

TASK = TaskId(uuid4())
STEP = StepId(uuid4())
PROJECT = ProjectId(uuid4())
GATE = GateId(uuid4())
ARTIFACT = ArtifactId(uuid4())
MEMORY = MemoryId(uuid4())
NOW = dt.datetime(2026, 7, 14, 6, 30, tzinfo=dt.UTC)
ACTOR = ev.ActorRef(type="user", id=ActorId(uuid4()), name="ana")


class DurableEnv(TypedDict):
    """The Lane-1 envelope, as kwargs. A plain dict would erase every id back to `object` under
    strict typing - and the whole point of `schemas/ids.py` is that a ProjectId is not a TaskId."""

    task_id: TaskId
    step_id: StepId
    project_id: ProjectId
    at: dt.datetime
    seq: int
    actor: ev.ActorRef


class LiveEnv(TypedDict):
    task_id: TaskId
    step_id: StepId
    at: dt.datetime
    after_seq: int
    channel: str


_DURABLE: DurableEnv = {
    "task_id": TASK,
    "step_id": STEP,
    "project_id": PROJECT,
    "at": NOW,
    "seq": 7,
    "actor": ACTOR,
}
_LIVE: LiveEnv = {
    "task_id": TASK,
    "step_id": STEP,
    "at": NOW,
    "after_seq": 7,
    "channel": f"{STEP}:narration",
}


class PresenceEnv(TypedDict):
    """Presence is about the task and the room, never a step - no step_id."""

    task_id: TaskId
    project_id: ProjectId
    at: dt.datetime
    conn: str
    actor: ev.ActorRef


_PRESENCE: PresenceEnv = {
    "task_id": TASK,
    "project_id": PROJECT,
    "at": NOW,
    "conn": "tab_7f3a9c21",
    "actor": ACTOR,
}

QUESTIONS = QuestionSet(
    title="Two things before I start",
    context="The brief is ambiguous about currency and audience.",
    questions=[
        Question(id="currency", prompt="Which currency?", type=QuestionType.TEXT, default="USD"),
        Question(id="ship", prompt="Ship on Friday?", type=QuestionType.BOOLEAN),
    ],
    expires_at=NOW + dt.timedelta(hours=24),
)

# One instance of every member of the union. A new event type with no line here fails the
# exhaustiveness test below - which is the point.
SAMPLES: Sequence[BaseModel] = [
    ev.TaskCreated(**_DURABLE, goal="write a memo", mode=TaskMode.STANDARD),
    ev.TaskClarifying(**_DURABLE, gate_id=GATE),
    ev.TaskPlanned(
        **_DURABLE,
        plan=PlanView(
            steps=[PlanStepView(id=STEP, title="draft", kind=StepKind.STEP, state=StepState.READY, attempt=1)]
        ),
    ),
    ev.TaskStatusChanged(**_DURABLE, status=TaskStatus.WAITING, reason="gate"),
    ev.TaskMoved(**_DURABLE, from_project_id=None, to_project_id=PROJECT),
    ev.TaskBudgetSet(**_DURABLE, usd=Decimal("2.00"), previous_usd=Decimal("0.01")),
    ev.MachineCreated(**_DURABLE, profile="base", seeded_as_of=NOW),
    ev.MachineSlept(**_DURABLE, reason="idle", after_s=600),
    ev.MachineWoke(**_DURABLE, turn=2, profile="browser", browser_restored=False, from_park=True),
    ev.MachineRebuilt(**_DURABLE, reason="expired", retention_days=14),
    ev.MachineReset(**_DURABLE),
    ev.MachineControlClaimed(**_DURABLE, private=True),
    ev.MachineControlEnded(
        **_DURABLE,
        reason="ended",
        held_s=100,
        ended_by=ev.ActorRef(type="user", id=ActorId(uuid4()), name="Ada"),
    ),
    ev.TaskRenamed(**_DURABLE, title="Launch memo"),
    ev.SteerApplied(
        **_DURABLE,
        command_id=UUID(int=7),
        step_title="Draft report",
        author="Ana",
        safe_point=SafePoint.BEFORE_TOOL,
    ),
    ev.SteerRejected(**_DURABLE, command_id=UUID(int=8), reason="the task was cancelled before this applied"),
    ev.SteerRetracted(**_DURABLE, command_id=UUID(int=9)),
    ev.PauseApplied(**_DURABLE, safe_point=SafePoint.BOUNDARY),
    ev.TaskCancelled(**_DURABLE, reason="user"),
    ev.TaskDelivered(**_DURABLE, summary="done", artifact_ids=[ARTIFACT]),
    # The turn's own dials ride the frame (deliverable joined) -
    # planning reads the EVENT, never the row's stamp, to know what the user chose.
    ev.TaskContinued(
        **_DURABLE,
        message="now shorten it",
        turn=2,
        client_token="gesture-2",
        scale="extended",
        deliverable="deck",
    ),
    # The Lead's offer: a proposal card under a reply, nothing executed.
    ev.TaskProposed(
        **_DURABLE,
        goal="a one-page memo on the Eddington correction",
        deliverable="document",
        why="it wants sources",
        turn=3,
    ),
    ev.TaskFeedback(**_DURABLE, rating="up", turn=2),
    ev.PlanEdited(
        **_DURABLE,
        edit=PlanEdit(kind=PlanEditKind.STEER, reason="currency changed", amended=[STEP], at=NOW),
    ),
    ev.StepStateChanged(**_DURABLE, state=StepState.RUNNING, title="draft", agent="writer"),
    ev.StepNarration(**_DURABLE, markdown="## What I did\nI wrote it."),
    ev.StepPrompt(**_DURABLE, attempt=1, agent="contract-reviewer", prompt="# Your job\n…", truncated=False),
    ev.ToolCalled(**_DURABLE, tool="frame.exec", ok=False, denied_reason="egress: example.com"),
    ev.GateOpened(**_DURABLE, gate_id=GATE, kind=GateKind.INPUT, title="Two things", questions=QUESTIONS),
    ev.GateResolved(
        **_DURABLE, gate_id=GATE, action=GateAction.ACCEPT, content={"currency": "EUR", "ship": True}
    ),
    ev.UsageRecorded(
        **_DURABLE,
        provider="anthropic",
        model="a-model",
        family=Family("anthropic"),
        units={Unit.INPUT_TOKEN: 1200, Unit.OUTPUT_TOKEN: 300},
        cost_usd=Decimal("0.0123"),
    ),
    ev.ArtifactProduced(
        **_DURABLE,
        artifact_id=ARTIFACT,
        artifact_type=ArtifactType.DOCUMENT,
        title="memo",
        sha256="a" * 64,
        bytes=2048,
    ),
    ev.ArtifactRevised(
        **_DURABLE,
        artifact_id=ARTIFACT,
        supersedes_id=ARTIFACT,
        lineage_id=ARTIFACT,
        revision=2,
        artifact_type=ArtifactType.COLLECTION,
        title="Workstreams",
        sha256="b" * 64,
        bytes=2199,
    ),
    ev.ArtifactSnapshot(**_DURABLE, path="/artifacts/memo.md", offset=8192, content="# Memo"),
    ev.VerificationSigned(
        **_DURABLE,
        artifact_id=ARTIFACT,
        verdict=Verdict.PASS,
        suite=VerifierSuite.DOCUMENT,
        verifier_family=Family("openai"),
        signature="c2ln",
    ),
    ev.QAReviewed(
        **_DURABLE,
        verdict="revise",
        attempt=1,
        findings=[
            QAFinding(
                location="section 07 'Risk'",
                severity=QASeverity.BLOCKER,
                kind=QAFindingKind.TRUNCATION,
                instruction="Restore the quotation cut mid-sentence at the section's end.",
            )
        ],
        summary="One truncation blocks; everything else holds.",
        reviewer_model="claude-opus-4-8",
        reviewer_family=Family("anthropic"),
    ),
    ev.EscalationRaised(
        **_DURABLE,
        diagnosis=Diagnosis(what_failed="recompute", why="B7 was hardcoded", attempts=3),
    ),
    ev.ContextAssembled(
        **_DURABLE,
        lanes=[
            ContextLaneView(
                lane="user",
                tokens=6,
                truncated=True,
                items=[ContextItemView(preview="Prefers numbers tables.", memory_id=MEMORY)],
            )
        ],
        total_tokens=6,
        memory_ids=[MEMORY],
    ),
    ev.MemoryWritten(
        **_DURABLE,
        count=1,
        items=[ev.MemoryStub(id=MEMORY, kind="preference", text="Prefers numbers tables.")],
    ),
    ev.BotMemoryWritten(
        **_DURABLE,
        bot="contract-reviewer",
        count=1,
        items=[ev.MemoryStub(id=MEMORY, kind="fact", text="The Q3 workbook lives in finance.")],
    ),
    ev.MessageStart(**_LIVE, agent="writer"),
    ev.MessageDelta(**_LIVE, text="The memo begins"),
    ev.MessageEnd(**_LIVE),
    ev.ArtifactDelta(**_LIVE, path="/work/memo.md", text="## Section"),
    ev.Progress(**_LIVE, phase="thinking", elapsed_s=3.2, tokens_so_far=180),
    # The micro-activity fact (the progress tree): one tool call's `end`,
    # label carrying a locator only - the Rule-9 posture the docstring promises.
    ev.Activity(
        **_LIVE,
        kind="tool",
        phase="end",
        tool="frame.shell",
        label="python3 /work/build.py",
        tool_call_id="tc1",
        ok=True,
        duration_ms=1200,
    ),
    ev.PresenceJoin(**_PRESENCE),
    ev.PresenceLeave(**_PRESENCE),
    ev.PresenceHere(**_PRESENCE, editor=False),
    ev.PresenceTyping(**_PRESENCE, on=True, mode="steer"),
    ev.ScreenState(task_id=TASK, project_id=PROJECT, at=NOW, holder=ACTOR, private=True, until=NOW),
    ev.ScreenAction(
        task_id=TASK,
        step_id=STEP,
        project_id=PROJECT,
        at=NOW,
        verb="click",
        box=ev.ScreenBox(x=412.0, y=230.5, w=96.0, h=32.0),
        host="admin.example.com",
        path="/users",
    ),
]


# ── a collection: legal, then mutated ──

FIXTURE: dict[str, Any] = {
    "collection_version": 1,
    "title": "Q3 workstreams",
    "fields": [
        {"key": "name", "label": "Name", "type": "text"},
        {"key": "stage", "label": "Stage", "type": "select", "options": ["Todo", "Doing", "Done"]},
        {"key": "tags", "label": "Tags", "type": "multi_select", "options": ["infra", "gtm"]},
        {"key": "due", "label": "Due", "type": "date"},
        {"key": "start", "label": "Start", "type": "date"},
        {"key": "owner", "label": "Owner", "type": "person"},
        {"key": "site", "label": "Site", "type": "url"},
        {"key": "cost", "label": "Cost", "type": "number", "unit": "$"},
        {"key": "shipped", "label": "Shipped", "type": "checkbox"},
    ],
    "rows": [
        {
            "id": "r1",
            "cells": {
                "name": "Line expansion",
                "stage": "Doing",
                "tags": ["infra"],
                "start": "2026-07-01",
                "due": "2026-08-01",
                "owner": "Dana",
                "site": "https://example.com",
                "cost": 41.6,
                "shipped": False,
            },
        },
        {"id": "r2", "cells": {"name": "Field hiring", "stage": "Todo", "due": "2026-09-15"}},
    ],
    "views": [
        {"name": "Board", "layout": "board", "group_by": "stage", "column_width": 320},
        {"name": "All", "layout": "table", "columns": ["name", "stage", "due"], "widths": {"name": 320}},
        {"name": "Timeline", "layout": "gantt", "start_field": "start", "end_field": "due"},
        {"name": "Deadlines", "layout": "calendar", "date_field": "due"},
        {"name": "Gallery", "layout": "gallery", "cover_field": "site"},
        {"name": "List", "layout": "list", "sort": [{"field": "due", "direction": "asc"}]},
    ],
}


def mutate(path: str, value: Any) -> dict[str, Any]:
    """FIXTURE with one node replaced - `views.0.group_by = missing`, `rows.1.id = r1`, …"""
    doc = copy.deepcopy(FIXTURE)
    node: Any = doc
    parts = path.split(".")
    for part in parts[:-1]:
        node = node[int(part)] if isinstance(node, list) else node[part]
    last = parts[-1]
    if isinstance(node, list):
        node[int(last)] = value
    else:
        node[last] = value
    return doc


MUTATIONS = {
    "a dangling board group_by": mutate("views.0.group_by", "missing"),
    "a group_by on a free-text field": mutate("views.0.group_by", "name"),
    "an illegal select value": mutate("rows.1.cells", {"name": "x", "stage": "Shipped"}),
    "a multi_select outside its options": mutate("rows.0.cells", {"tags": ["nope"]}),
    "a bad date": mutate("rows.1.cells", {"due": "next tuesday"}),
    "a date outside 1900-2100": mutate("rows.1.cells", {"due": "1899-12-31"}),
    "a duplicate row id": mutate("rows.1.id", "r1"),
    "a cell for an unknown field": mutate("rows.1.cells", {"nope": "x"}),
    "a duplicated field key": mutate("fields.1", {"key": "name", "label": "N", "type": "text"}),
    "options on a non-select": mutate(
        "fields.0", {"key": "name", "label": "N", "type": "text", "options": ["a"]}
    ),
    "a select without options": mutate("fields.1", {"key": "stage", "label": "S", "type": "select"}),
    "a gantt without end_field": mutate(
        "views.2", {"name": "Timeline", "layout": "gantt", "start_field": "start"}
    ),
    "a calendar without date_field": mutate("views.3", {"name": "Deadlines", "layout": "calendar"}),
    "a gallery cover that is not a url field": mutate(
        "views.4", {"name": "Gallery", "layout": "gallery", "cover_field": "name"}
    ),
    "a wrong version": mutate("collection_version", 2),
    "a number cell holding a string": mutate("rows.0.cells", {"cost": "41.6"}),
    "a checkbox holding a string": mutate("rows.0.cells", {"shipped": "yes"}),
    "a widths key that is not a field": mutate("views.1.widths", {"nope": 300}),
    "a width below 80": mutate("views.1.widths", {"name": 20}),
    "a non-numeric width": mutate("views.1.widths", {"name": "wide"}),
    "a column_width outside 200-600": mutate("views.0.column_width", 5000),
}
