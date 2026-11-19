# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The source store: a page `web.fetch` read is kept, sealed, by the sha its `tool.called` row holds as `ref`.

The receipt checks a web quote against the kept page (the REAL document suite, on a frame whose
roots are folders on this host), a follow-up mounts `sources.jsonl` beside its earlier
deliverables, and the pages go when the task is deleted.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import orjson
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid import storage
from prismoid.ai.tools.labels import fetch_label
from prismoid.db.base import utcnow
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.session import session_scope
from prismoid.operations import sources, verification
from prismoid.operations import tasks as task_ops
from prismoid.operations.execution.mounts import materialise_inputs
from prismoid.schemas.events import ToolCalled
from prismoid.schemas.ids import StepId, TaskId
from prismoid.schemas.model_class import Family
from prismoid.schemas.plan import TaskStatus
from prismoid.schemas.verification import JudgeOpinion, Verdict
from tests.support.artifacts import deliverable, verified
from tests.support.cast import member
from tests.support.host_frame import HostFrame

pytestmark = pytest.mark.integration

URL = "https://example.com/q3-report?utm=mail"
PAGE = (
    "# Q3 report\n\nRevenue grew 14.2 percent to 31 million euros in the third quarter, "
    "the board said on Tuesday.\n"
)


@pytest.fixture
def host_frame(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> HostFrame:
    """Verification on this host, judged PASS by a stub judge: the deterministic floor decides."""
    frame = HostFrame(tmp_path)

    async def judge(*args: Any, **kwargs: Any) -> JudgeOpinion:
        return JudgeOpinion(verdict=Verdict.PASS, rationale="fine", model="google:g", family=Family("google"))

    monkeypatch.setattr(verification, "new_frame", lambda *a, **k: frame)
    monkeypatch.setattr(verification, "ensure_ready", lambda profile="base": None)
    monkeypatch.setattr(verification, "judge", judge)
    return frame


class _Frame:
    """The frame's write side: what a step could read, and how it was written."""

    def __init__(self) -> None:
        self.files: dict[str, tuple[bytes, int, str]] = {}

    async def write_bytes(self, path: str, data: bytes, *, mode: int, owner: str) -> None:
        self.files[path] = (data, mode, owner)


async def _read_and_keep(s: AsyncSession, task: Task, step: PlanStep) -> str:
    """One `web.fetch` as a step run records it: the page kept, its sha on the row."""
    keeper = sources.keeper(task.id)
    sha = await keeper(PAGE)
    assert await keeper.read(sha) == PAGE, "a resumed run reads the page back by its sha"
    await TaskEvent.append(
        s,
        ToolCalled(
            seq=await task.next_seq(s),
            task_id=TaskId(task.id),
            step_id=StepId(step.id),
            at=utcnow(),
            tool="web.fetch",
            ok=True,
            summary=fetch_label(URL),
            ref=sha,
        ),
    )
    return sha


async def _task(s: AsyncSession, name: str) -> tuple[Any, Any, Task, PlanStep]:
    actor, workspace = await member(s, name)
    made = await task_ops.create_task(s, actor, goal="Read the Q3 report and write a memo")
    task = await s.get(Task, made.id)
    assert task is not None
    step = PlanStep(task_id=task.id, ordinal=0, title="write the memo")
    s.add(step)
    await s.flush()
    return actor, workspace, task, step


async def _quotes(task_id: uuid.UUID, step_id: uuid.UUID, memo: bytes, workspace_id: uuid.UUID) -> Any:
    async with session_scope() as s:
        artifact = deliverable(task_id, memo, step_id=step_id, title="Q3 memo")
        s.add(artifact)
        await s.commit()
    signed = await verified(artifact.id, workspace_id)
    return next(c for c in signed.report.checks if c.name == "quotes:verbatim")


async def test_a_web_quote_is_checked_against_the_page_that_was_read(
    silent_dispatcher: None, host_frame: HostFrame
) -> None:
    async with session_scope() as s:
        _, workspace, task, step = await _task(s, "Ada")
        ids = (task.id, step.id)  # read while the session is open: it closes before the checks
        workspace_id = workspace.id
        sha = await _read_and_keep(s, task, step)
        await s.commit()
        [row] = await TaskEvent.of_types(s, task.id, ("tool.called",))
        assert isinstance(row, ToolCalled) and (row.summary, row.ref) == ("example.com/q3-report", sha)

    faithful = await _quotes(
        *ids,
        b'The board: "revenue grew 14.2 percent to 31 million euros in the third quarter".\n',
        workspace_id,
    )
    assert faithful.status.value == "pass", faithful
    assert host_frame.job["sources"] == [str(Path(host_frame.paths.task) / f"inputs/page-{sha[:16]}.md")]

    paraphrase = await _quotes(
        *ids,
        b'The board said "sales rose about fourteen percent in the summer quarter".\n',
        workspace_id,
    )
    assert paraphrase.status.value == "fail", paraphrase


async def test_a_follow_up_mounts_sources_jsonl_and_a_delete_takes_the_pages(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        actor, _, task, step = await _task(s, "Bo")
        sha = await _read_and_keep(s, task, step)
        task.transition(TaskStatus.PLANNING)
        task.transition(TaskStatus.RUNNING)
        task.transition(TaskStatus.DELIVERED)
        await s.commit()

        first = _Frame()
        await materialise_inputs(s, task, first)
        assert "/task/inputs/sources.jsonl" not in first.files, "turn 1 has no earlier turn to trace"

        await task_ops.continue_task(s, actor, task.id, goal="where did the 14.2 come from?")
        follow_up = _Frame()
        mounted = await materialise_inputs(s, task, follow_up)
        ledger, mode, owner = follow_up.files["/task/inputs/sources.jsonl"]
        assert [orjson.loads(line) for line in ledger.splitlines()] == [
            {"page": "example.com/q3-report", "sha256": sha}
        ]
        assert (mode, owner) == (0o444, "system")
        assert "/task/inputs/sources.jsonl" not in mounted, "a ledger, never a declared source"

        assert await storage.alist_keys(f"sources/{task.id}/") == [f"sources/{task.id}/{sha}"]
        task.transition(TaskStatus.CANCELLED)
        await s.commit()
        await task_ops.delete_task(s, actor, task.id)
    assert await storage.alist_keys(f"sources/{task.id}/") == []


async def test_a_follow_up_and_a_fork_carry_the_source_ids_a_revision_cites(silent_dispatcher: None) -> None:
    """The ledger names each page by the id the task gave it; a fork gets the parent's ids and pages, so a
    revision of the parent's document cites `[S1]` and its quote is checked against the same page."""
    async with session_scope() as s:
        actor, _, task, step = await _task(s, "Cy")
        sha = await _read_and_keep(s, task, step)
        assert await sources.keeper(task.id).cite(URL, sha) == "S1"
        task.transition(TaskStatus.PLANNING)
        task.transition(TaskStatus.RUNNING)
        task.transition(TaskStatus.DELIVERED)
        await s.commit()
        line = {"id": "S1", "page": "example.com/q3-report", "sha256": sha}

        await task_ops.continue_task(s, actor, task.id, goal="add the Q2 numbers")
        follow_up = _Frame()
        await materialise_inputs(s, task, follow_up)
        assert orjson.loads(follow_up.files["/task/inputs/sources.jsonl"][0]) == line

        child = await task_ops.fork(s, actor, task.id)
        fork = _Frame()
        await materialise_inputs(s, child, fork)
        assert orjson.loads(fork.files["/task/inputs/sources.jsonl"][0]) == line
        assert await sources.source_ids(child.id) == {"S1": URL}
        assert await sources.keeper(child.id).recall("S1") == PAGE
        assert list(await sources.pages(s, child.id)) == [f"{sources.PAGE_PREFIX}{sha[:16]}.md"]
