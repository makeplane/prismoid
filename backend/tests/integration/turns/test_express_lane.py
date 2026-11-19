# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The express lane as the Lead's (ADR-049, ADR-092), pinned end to end minus the queues: every new turn
mints the Lead's frameless one-step plan with no model before it; its run needs no docker frame,
publishes nothing, and delivers its reply as the answer, with the routed record on the stream.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic_ai.messages import ModelRequest, UserPromptPart

import prismoid.ai.agents.base as agents_base
from prismoid.ai.deps import TaskDeps
from prismoid.ai.tools import fetch
from prismoid.ai.tools.research import Shelf
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope, session_scope
from prismoid.operations import execution, planning, sources
from prismoid.operations import tasks as task_ops
from prismoid.schemas.events import TaskPlanned, ToolCalled
from prismoid.schemas.plan import StepState, TaskStatus
from tests.support.cast import member
from tests.support.fetched import on_the_wire
from tests.support.runners import scripted

pytestmark = pytest.mark.integration


async def _minted_express(monkeypatch: pytest.MonkeyPatch, goal: str):
    async with session_scope() as session:
        actor, _ = await member(session, "Asker")
        await session.flush()
        task = await task_ops.create_task(session, actor, goal=goal, clarify="never")
        task.transition(TaskStatus.PLANNING)
        await session.flush()
        view = await planning.draft_plan(session, task.id)
        await session.commit()
        return task.id, view


async def test_whats_the_time_mints_the_leads_frameless_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id, view = await _minted_express(monkeypatch, "What's the time?")

    assert len(view.steps) == 1
    assert view.steps[0].agent == "lead"
    assert view.steps[0].lane_role == "express"

    async with row_scope(Task, task_id) as session:
        planned = await TaskEvent.latest_of_type(session, task_id, "task.planned")
    assert isinstance(planned, TaskPlanned)
    assert planned.route == "express"
    assert planned.route_source == "lead"
    assert planned.route_why  # wear the reason - never empty


async def test_express_runs_without_a_frame_and_delivers_the_narration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id, view = await _minted_express(monkeypatch, "What's the time?")

    prompts: list[str] = []

    async def canned_run(agent: object, prompt: str, deps: object, **kwargs: object) -> object:
        # The ambient facts must be in the brief - "what's the time?" is answerable AND right.
        assert "Ambient facts" in prompt and "current UTC time" in prompt
        prompts.append(prompt)
        return SimpleNamespace(output="It is 14:02 UTC (ambient).")

    monkeypatch.setattr(agents_base, "build", scripted(canned_run))

    async def exploding_frame(*args: object, **kwargs: object) -> object:
        raise AssertionError("an express step must never create a frame (ADR-049 §3)")

    import prismoid.ai.frames as frames

    monkeypatch.setattr(frames, "new_frame", exploding_frame)

    async with row_scope(Task, task_id) as session:
        await execution.advance(session, task_id, TaskStatus.RUNNING)
        await session.commit()

    async with row_scope(Task, task_id) as session:
        outcome = await execution.execute_step(session, task_id, uuid.UUID(str(view.steps[0].id)))
    assert outcome == "It is 14:02 UTC (ambient)."
    # The Lead is told who is asking now, under its own heading (the asker's
    # name is quoted data, never spliced into the thread block).
    assert '# Who is asking\n\nThe person asking now is "Asker".' in prompts[0]

    async with row_scope(Task, task_id) as session:
        await execution.deliver(session, task_id, outcome)
        await session.commit()

    async with row_scope(Task, task_id) as session:
        from prismoid.db.models.artifact import Artifact
        from prismoid.db.models.plan import PlanStep

        task = await session.get(Task, task_id)
        assert task is not None and task.state is TaskStatus.DELIVERED
        steps = await PlanStep.for_task(session, task_id)
        assert steps[0].step_state is StepState.DONE
        produced = await Artifact.for_step(session, steps[0].id)
        assert produced == [], "express publishes nothing - the narration IS the answer"


# Whether a turn reads the live web is the Lead's call now, not a door's tier: the `live.fresh` rows of
# `evals/cases/lead_parity.py` (range:lookup-plain-weather) pin it.


async def test_a_doing_ask_is_the_leads_turn_too(monkeypatch: pytest.MonkeyPatch) -> None:
    task_id, view = await _minted_express(monkeypatch, "Analyze our churn and tell me what to fix")
    assert [(s.agent, s.lane_role) for s in view.steps] == [("lead", "express")]
    async with row_scope(Task, task_id) as session:
        planned = await TaskEvent.latest_of_type(session, task_id, "task.planned")
    assert isinstance(planned, TaskPlanned) and planned.route_source == "lead"


async def test_a_teammates_turn_is_answered_in_the_teammates_timezone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ambient facts are the ASKER's: on Bob's turn "what's the time?" means
    his clock, not the creator's - the turn's author, as the "Who is asking" line names him."""
    from prismoid.db.models.user import User
    from prismoid.db.models.workspace import WorkspaceRole

    task_id, view = await _minted_express(monkeypatch, "What's the time?")
    async with row_scope(Task, task_id) as session:
        task = await session.get(Task, task_id)
        assert task is not None
        bob, _ = await member(session, "Bob", role=WorkspaceRole.MEMBER)
        for who, tz in ((task.creator_actor_id, "Europe/Lisbon"), (bob.id, "Asia/Kolkata")):
            session.add(
                User(actor_id=who, email=f"u{uuid.uuid4().hex[:12]}@example.com", name="n", timezone=tz)
            )
        task.turn_actor_id = bob.id
        await session.commit()

    prompts: list[str] = []

    async def canned_run(agent: object, prompt: str, deps: object, **kwargs: object) -> object:
        prompts.append(prompt)
        return SimpleNamespace(output="It is evening in Kolkata.")

    monkeypatch.setattr(agents_base, "build", scripted(canned_run))
    async with row_scope(Task, task_id) as session:
        await execution.advance(session, task_id, TaskStatus.RUNNING)
        await session.commit()
    async with row_scope(Task, task_id) as session:
        await execution.execute_step(session, task_id, uuid.UUID(str(view.steps[0].id)))

    assert "- the user's timezone: Asia/Kolkata" in prompts[0], "Bob's clock, on Bob's turn"


async def _public_dns(host: str, port: int) -> list[str]:
    """DNS as one public address: the read never leaves the test."""
    return ["93.184.216.34"]


async def test_an_express_answer_keeps_the_pages_it_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """A page the Lead fetched is stored like a step's, and its `tool.called` row carries the sha."""
    task_id, view = await _minted_express(monkeypatch, "What's the time?")
    page = "# Clock\n\nIt is 14:02 UTC.\n"
    kept: list[str | None] = []

    async def reading_run(agent: object, prompt: str, deps: TaskDeps, **kwargs: object) -> object:
        # `web_fetch` on the deps' ports: the keep stores the text, the journal says the sha as `ref`.
        served = httpx.MockTransport(
            on_the_wire(lambda _: httpx.Response(200, text=page, headers={"content-type": "text/markdown"}))
        )
        toolset = fetch.fetch_toolset(
            (fetch.TOOL,),
            deps.journal_tool,
            client_factory=lambda: httpx.AsyncClient(transport=served),
            resolver=_public_dns,
            shelf=Shelf(keep=deps.keep_page),
        )
        assert toolset is not None
        web_fetch: Any = toolset.tools["web_fetch"].function  # called with the slice of RunContext it reads
        url = "https://time.is/UTC?x=1"
        ctx = SimpleNamespace(messages=[ModelRequest(parts=[UserPromptPart(url)])], tool_call_id="toolu_1")
        read = await web_fetch(ctx, url)
        kept.append(read.metadata.sha)
        return SimpleNamespace(output="It is 14:02 UTC.")

    monkeypatch.setattr(agents_base, "build", scripted(reading_run))
    async with row_scope(Task, task_id) as session:
        await execution.advance(session, task_id, TaskStatus.RUNNING)
        await session.commit()
    async with row_scope(Task, task_id) as session:
        await execution.execute_step(session, task_id, uuid.UUID(str(view.steps[0].id)))

    [sha] = kept
    assert sha is not None, "the express deps carry no keep_page port"
    assert await sources.keeper(task_id).read(sha) == page
    async with row_scope(Task, task_id) as session:
        rows = await TaskEvent.of_types(session, task_id, ("tool.called",))
        assert [(r.summary, r.ref) for r in rows if isinstance(r, ToolCalled)] == [("time.is/UTC", sha)]
        assert [p.sha256 for p in await sources.stored(session, task_id)] == [sha]
