# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The receipt reads the task's journal: every page the task read reaches the document suite.

`links:resolve` used to hold a link only to the producing step's egress allowlist, and a page read
through the worker's `web.fetch` (or the browser, under a person's grant) is never on it. Now the
verifier gathers the task's read pages - every `ok=true` `web.fetch` and every page the browser
loaded, from any step and any turn - and hands them to the suite in its job. These tests run the
REAL document suite on a frame whose roots are folders on this host, so the job asserted is the
job a frame reads and the check asserted is the one the suite wrote.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from prismoid.ai.frames.driver import ExecResult
from prismoid.ai.frames.layout import FramePaths
from prismoid.db.base import utcnow
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.session import session_scope
from prismoid.operations import tasks as task_ops
from prismoid.operations import verification
from prismoid.schemas.events import TaskContinued, ToolCalled
from prismoid.schemas.ids import StepId, TaskId
from prismoid.schemas.model_class import Family
from prismoid.schemas.verification import JudgeOpinion, Verdict
from tests.support.artifacts import deliverable, verified
from tests.support.cast import member
from tests.support.paths import TESTS

pytestmark = pytest.mark.integration

FIXTURES = TESTS / "fixtures" / "go_memo"


class _HostFrame:
    """A verify frame on this host: each virtual root is a folder under `root`, and the suite runs
    for real through the system-Python door, so nothing between the journal and the check is
    stubbed but the sandbox itself."""

    def __init__(self, root: Path) -> None:
        def at(name: str) -> str:
            return str(root / name)

        self.paths = FramePaths(
            task=at("task"),
            work=at("work"),
            artifacts=at("artifacts"),
            skills=at("skills"),
            verify=at("verify"),
            inputs=at("task/inputs"),
            browser=at("browser"),
            on_host=True,
        )
        self.job: dict[str, Any] = {}

    async def create(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def write_bytes(self, path: str, data: bytes, *, mode: int = 0o644, owner: str = "agent") -> None:
        _put(Path(self.paths.render(path)), data)
        if path.endswith("/job.json"):
            self.job = json.loads(data)

    async def read_bytes(self, path: str) -> bytes:
        raise FileNotFoundError(path)

    async def system_python(
        self,
        script: str,
        *args: str,
        stdin: str | None = None,
        timeout: float = 120.0,  # noqa: ASYNC109
    ) -> ExecResult:
        body = _get(Path(self.paths.render(stdin))) if stdin else b""
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            self.paths.render(script),
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(body), timeout)
        return ExecResult(proc.returncode or 0, out.decode(), err.decode())

    async def destroy(self) -> None:
        return None


def _put(real: Path, data: bytes) -> None:
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_bytes(data)


def _get(real: Path) -> bytes:
    return real.read_bytes()


@pytest.fixture
def host_frame(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _HostFrame:
    """Verification on this host, judged PASS by a stub judge: the deterministic floor decides."""
    frame = _HostFrame(tmp_path)

    async def judge(*args: Any, **kwargs: Any) -> JudgeOpinion:
        return JudgeOpinion(verdict=Verdict.PASS, rationale="fine", model="google:g", family=Family("google"))

    monkeypatch.setattr(verification, "new_frame", lambda *a, **k: frame)
    monkeypatch.setattr(verification, "ensure_ready", lambda profile="base": None)
    monkeypatch.setattr(verification, "judge", judge)
    return frame


async def _call(
    s: Any, task: Task, step: PlanStep | None, tool: str, summary: str, *, ok: bool = True
) -> None:
    await TaskEvent.append(
        s,
        ToolCalled(
            seq=await task.next_seq(s),
            task_id=TaskId(task.id),
            step_id=StepId(step.id) if step else None,
            at=utcnow(),
            tool=tool,
            ok=ok,
            summary=summary,
            attempt=step.attempt if step else None,
        ),
    )


async def _task(s: Any, actor: Any, goal: str) -> tuple[Task, PlanStep, PlanStep]:
    made = await task_ops.create_task(s, actor, goal=goal)
    task = await s.get(Task, made.id)
    assert task is not None
    research = PlanStep(task_id=task.id, ordinal=0, title="read the release notes")
    write = PlanStep(task_id=task.id, ordinal=1, title="write the memo")
    s.add_all([research, write])
    await s.flush()
    return task, research, write


async def _verify(task: Task, step: PlanStep, memo: bytes, workspace_id: uuid.UUID) -> Any:
    async with session_scope() as s:
        artifact = deliverable(task.id, memo, step_id=step.id, title="Go release memo")
        s.add(artifact)
        await s.commit()
    return await verified(artifact.id, workspace_id)


def _links(signed: Any) -> Any:
    return next(c for c in signed.report.checks if c.name == "links:resolve")


async def test_pages_read_in_any_step_and_any_turn_reach_the_suite(
    silent_dispatcher: None, host_frame: _HostFrame
) -> None:
    """A writer may cite what an earlier researcher step fetched, and a later turn what the first
    turn's browser opened. A fetch that failed is no read, and another task's reads are not this
    task's."""
    async with session_scope() as s:
        actor, workspace = await member(s, "Ada")
        task, research, write = await _task(s, actor, "Search the web for Go release notes and write a memo")
        other, other_step, _ = await _task(s, actor, "something else")
        await _call(s, task, research, "web.fetch", "go.dev/doc/devel/release")
        await _call(s, task, research, "web.fetch", "go.dev/doc/go1.28", ok=False)
        await _call(s, task, research, "web.search", "5 results")
        await _call(s, task, research, "browser.click", 'pkg.go.dev/vuln · link "GO-2026-6859"')
        await TaskEvent.append(
            s,
            TaskContinued(
                seq=await task.next_seq(s),
                task_id=TaskId(task.id),
                at=utcnow(),
                message="add downloads",
                turn=2,
            ),
        )
        write.attempt = 2
        await _call(s, task, write, "browser.snapshot", "go.dev/dl")
        await _call(s, task, None, "browser.open", "go.dev/blog/go1.27")
        await _call(s, other, other_step, "web.fetch", "example.org/private")
        await s.commit()

    memo = (
        b"# Go\n\nSee [the release history](https://go.dev/doc/devel/release#go1.27.minor), "
        b"[the downloads](https://go.dev/dl/), [the report](https://pkg.go.dev/vuln?q=net) and "
        b"[the post](https://go.dev/blog/go1.27).\n"
    )
    signed = await _verify(task, write, memo, workspace.id)

    assert host_frame.job["read_pages"] == [
        "go.dev/blog/go1.27",
        "go.dev/dl",
        "go.dev/doc/devel/release",
        "pkg.go.dev/vuln",
    ]
    assert _links(signed).status.value == "pass", _links(signed)


async def test_a_failed_fetch_or_another_tasks_read_still_fails_the_link(
    silent_dispatcher: None, host_frame: _HostFrame
) -> None:
    async with session_scope() as s:
        actor, workspace = await member(s, "Bo")
        task, research, write = await _task(s, actor, "write a memo")
        other, other_step, _ = await _task(s, actor, "something else")
        await _call(s, task, research, "web.fetch", "go.dev/doc/go1.28", ok=False)
        await _call(s, other, other_step, "web.fetch", "example.org/private")
        await s.commit()

    memo = b"See [1.28](https://go.dev/doc/go1.28) and [this](https://example.org/private).\n"
    signed = await _verify(task, write, memo, workspace.id)

    assert host_frame.job["read_pages"] == []
    check = _links(signed)
    assert check.status.value == "fail"
    assert "https://go.dev/doc/go1.28" in str(check.actual)
    assert "https://example.org/private" in str(check.actual)


async def test_the_go_memo_signs_links_resolve_pass(silent_dispatcher: None, host_frame: _HostFrame) -> None:
    """The live defect, end to end short of the model: one writer's 4 searches and 13 fetches on
    the record, its memo of 8 links, and `links:resolve` signs pass where it signed "0 of 8"."""
    journal = json.loads((FIXTURES / "journal.json").read_text())
    async with session_scope() as s:
        actor, workspace = await member(s, "Cy")
        task, _, write = await _task(
            s, actor, "Search the web for this week's Go release notes and write a memo"
        )
        for row in journal:
            await _call(s, task, write, row["tool"], row["summary"], ok=row["ok"])
        await s.commit()

    signed = await _verify(task, write, (FIXTURES / "memo.md").read_bytes(), workspace.id)

    check = _links(signed)
    assert (check.status.value, check.detail) == ("pass", "all 8 link(s) resolve")
