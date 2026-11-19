# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Task-wide source ids: a page gets one id the first time any step of the task reads it, the researcher's
findings keep their `[S#]` marks for the writer that reads them, and the writer's publish links the page."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from pydantic_ai.messages import ModelRequest, ToolReturnPart

from prismoid.ai.deps import TaskDeps
from prismoid.ai.tools.research import Shelf, WebRead
from prismoid.db.models.plan import PlanEdge, PlanStep
from prismoid.db.models.task import Task
from prismoid.db.session import session_scope
from prismoid.operations import sources
from prismoid.operations import tasks as task_ops
from prismoid.operations.execution.equip import Equipped, cited_later
from prismoid.operations.execution.inbox import StepInbox
from prismoid.operations.execution.publish import Output, publish_output
from prismoid.schemas.contract import Contract
from prismoid.schemas.ids import AgentName
from prismoid.schemas.outputs import ArtifactType, OutputRef, SubmittedDocument
from prismoid.schemas.plan import StepKind, StepSpec
from tests.support.cast import member

pytestmark = pytest.mark.integration

PAGES = ("https://example.com/a", "https://example.com/b", "https://example.com/q3?utm=mail")


class _Frame:
    def __init__(self) -> None:
        self.written: dict[str, bytes] = {}

    async def write_bytes(self, path: str, body: bytes) -> None:
        self.written[path] = body

    async def publish(self, path: str, type_: str, title: str, sources: list[str] | None = None) -> None:
        return None


class _Result:
    def __init__(self, output: Any, history: tuple[ModelRequest, ...] = ()) -> None:
        self.output, self._history = output, list(history)

    def all_messages(self) -> list[Any]:
        return self._history


async def _fact(phase: str, label: str, *, started: float | None = None) -> None:
    return None


async def _task(name: str) -> uuid.UUID:
    async with session_scope() as s:
        actor, _ = await member(s, name)
        made = await task_ops.create_task(s, actor, goal="Research the Q3 numbers and write a memo")
        await s.commit()
        return made.id


def _at(frame: _Frame, agent: str, deps: TaskDeps, *, later: bool) -> Output:
    spec = StepSpec(
        kind=StepKind.STEP,
        agent=AgentName(agent),
        title=agent,
        contract=Contract(
            goal="Do it.", acceptance=["done"], output=OutputRef(type=ArtifactType.DOCUMENT, title="Doc")
        ),
    )
    inbox = StepInbox(uuid.uuid4(), uuid.uuid4(), 1, told=0)
    equipped = Equipped(deps=deps, inputs=(), bots=(), inbox=inbox, cited_later=later)
    return Output(frame=frame, deps=deps, spec=spec, step=PlanStep(), equipped=equipped, fact=_fact)


async def test_a_page_two_steps_read_at_once_gets_one_task_wide_id(silent_dispatcher: None) -> None:
    task_id = await _task("Ada")
    researcher, writer = sources.keeper(task_id), sources.keeper(task_id)
    a, b = PAGES[0], PAGES[1]
    ids = await asyncio.gather(
        *(keep.cite(page, None) for keep in (researcher, writer) for page in (a, b, a))
    )
    assert sorted(set(ids)) == ["S1", "S2"], ids
    assert ids[0] == ids[2] == ids[3] == ids[5] != ids[1] == ids[4]
    assert await sources.source_ids(task_id) == {ids[0]: a, ids[1]: b}


async def test_the_writer_cites_the_researchers_read_and_the_final_artifact_links_it(
    silent_dispatcher: None,
) -> None:
    task_id = await _task("Bo")
    async with session_scope() as s:
        task = await s.get(Task, task_id)
        assert task is not None
        research = PlanStep(task_id=task_id, ordinal=0, title="research", spec={"agent": "researcher"})
        write = PlanStep(task_id=task_id, ordinal=1, title="write", spec={"agent": "writer"})
        s.add_all([research, write])
        await s.flush()
        s.add(PlanEdge(task_id=task_id, from_step_id=research.id, to_step_id=write.id))
        await s.flush()
        assert await cited_later(s, task, research) and not await cited_later(s, task, write)
        await s.commit()

    shelf = Shelf(sources.keeper(task_id), cites=True)
    assert [await shelf.put(page, "text") for page in PAGES] == ["S1", "S2", "S3"]
    reads = tuple(  # the researcher's reads as its history keeps them, each id on its return
        ModelRequest(
            parts=[
                ToolReturnPart("web_fetch", "page", tool_call_id=sid, metadata=WebRead(page=p, source=sid))
            ]
        )
        for p, sid in shelf.ids.items()
    )
    findings = _Frame()
    await publish_output(
        _at(findings, "researcher", TaskDeps(), later=True),
        _Result("# Findings\n\nRevenue rose 14% [S3].", reads),
        override=None,
    )
    assert "Revenue rose 14% [S3]." in next(iter(findings.written.values())).decode()

    memo = _Frame()
    deps = TaskDeps(sources=await sources.source_ids(task_id))
    done: Any = _Result(SubmittedDocument(title="Q3 memo", content="Revenue rose 14% [S3]."))
    await publish_output(_at(memo, "writer", deps, later=False), done, override=None)
    body = next(iter(memo.written.values())).decode()
    assert f"Revenue rose 14% [[1]](<{PAGES[2]}>)." in body and f"1. <{PAGES[2]}>" in body


async def test_a_later_step_reads_back_the_page_an_earlier_step_kept_under_its_id(
    silent_dispatcher: None,
) -> None:
    task_id = await _task("Cy")
    keep = sources.keeper(task_id)
    text = "The board said margins would stay flat next year."
    assert await keep.cite(PAGES[0], await keep(text)) == "S1"

    ids = {p: sid for sid, p in (await sources.source_ids(task_id)).items()}
    for _ in range(2):  # the writer's step, and that step run again
        assert await Shelf(keep, ids=dict(ids)).page("S1") == (PAGES[0], text)
    assert await Shelf(keep, ids=dict(ids)).page("S2") is None, "an id the task never gave"
