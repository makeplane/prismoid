# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The navigator end to end against a real browser frame and the fixture admin app, with a
scripted model: login through the masked field, a Delete that must ask, the fixture's log as
the record of what happened, and the recording verifying structurally."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai import Agent, DeferredToolRequests, DeferredToolResults, ToolApproved
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from prismoid.ai.agents.run import AgentRunner
from prismoid.ai.capabilities.durability import PrismoidDurability
from prismoid.ai.capabilities.taint import tainted
from prismoid.ai.frames.docker_driver import DockerFrame, ensure_image
from prismoid.ai.rules.approvals import Approvals
from prismoid.ai.tools.browser import BROWSER_TOOLS, BrowserPorts, browser_toolset
from prismoid.ai.verification.suites.recording import check_recording
from tests.support.cast import bound_scope, member
from tests.support.runners import runner_for

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("frames-d")]
FIXTURE = Path("tests/fixtures/admin")


@pytest.fixture
async def frame():
    ensure_image("browser")
    f = DockerFrame(task_id=f"pytest-nav-{uuid.uuid4().hex[:8]}", profile="browser", attempt=1)
    await f.create()
    for name in ("index.html", "records.html", "settings.html", "server.py"):
        await f.write_file(f"/work/site/{name}", (FIXTURE / name).read_text())
    await f.exec(
        "cd /work/site && (python3 server.py 8088 >/dev/null 2>&1 &) && for i in $(seq 1 40); do curl -sf --noproxy '*' http://127.0.0.1:8088/index.html >/dev/null && break; sleep 0.1; done"
    )
    yield f
    await f.destroy()


@pytest.fixture
async def machines_made():
    """Task ids whose Machine a real-engine test leaves warm: their frames go at teardown."""
    from prismoid.ai.frames import destroy_ref
    from prismoid.db.models.machine import Machine
    from prismoid.db.models.task import Task
    from prismoid.db.session import row_scope

    made: list[uuid.UUID] = []
    yield made
    for task_id in made:
        async with row_scope(Task, task_id) as db:
            row = await Machine.for_task(db, task_id)
            ref = row.frame_ref if row is not None else None
        if ref:
            await destroy_ref(ref)


def _returns(messages: list[ModelMessage]) -> list[ToolReturnPart]:
    return [p for m in messages for p in getattr(m, "parts", []) if isinstance(p, ToolReturnPart)]


def _ref(snapshot: str, needle: str) -> str:
    """The reference right before `needle` on its line: the look puts a small form on one line."""
    line = next(line for line in snapshot.splitlines() if needle in line)
    return re.findall(r"\[(e\d+\.\d+)\]", line[: line.index(needle)])[-1]


async def test_login_then_delete_asks_and_lands_only_after_approval(frame: DockerFrame) -> None:
    rows: list[tuple[str, bool, str]] = []

    async def journal(
        tool: str,
        ok: bool,
        summary: str = "",
        duration_ms: int = 0,
        denied_reason: str = "",
        tool_call_id: str = "",
        ref: str | None = None,
    ) -> None:
        rows.append((tool, ok, summary))

    ports = BrowserPorts(browse=frame.browser, allow=frame.allow, allowed={"127.0.0.1"})
    toolset = browser_toolset(BROWSER_TOOLS, journal, ports, Approvals(tainted=tainted))
    assert toolset is not None

    def script(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen = _returns(messages)
        n = len(seen)
        if n == 0:
            return ModelResponse(
                parts=[
                    ToolCallPart(tool_name="browser_open", args={"url": "http://127.0.0.1:8088/index.html"})
                ]
            )
        if n == 1:
            return ModelResponse(parts=[ToolCallPart(tool_name="browser_snapshot", args={})])
        if n == 2:
            snap = str(seen[1].content)
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="browser_fill",
                        args={
                            "fields": [
                                {"ref": _ref(snap, 'textbox "Username"'), "value": "admin"},
                                {"ref": _ref(snap, '"Password"'), "value": "hunter2"},
                            ],
                            "submit": True,
                        },
                    )
                ]
            )
        if n == 3:
            return ModelResponse(parts=[ToolCallPart(tool_name="browser_snapshot", args={})])
        if n == 4:
            snap = str(seen[3].content)
            assert "Grace Hopper" in snap and "hunter2" not in snap
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="browser_click", args={"ref": _ref(snap, 'button "Delete Grace Hopper"')}
                    )
                ]
            )
        return ModelResponse(parts=[TextPart(f"done: {seen[-1].content}")])

    agent: Agent[None, Any] = Agent(
        FunctionModel(script), output_type=[str, DeferredToolRequests], toolsets=[toolset]
    )
    first = await agent.run("Delete Grace Hopper's record")
    assert isinstance(first.output, DeferredToolRequests), "the Delete must ask"
    call_id = first.output.approvals[0].tool_call_id
    meta = first.output.metadata[call_id]
    assert meta["tool"] == "browser.click" and meta["fingerprint"]["name"] == "Delete Grace Hopper"

    log_before = json.loads((await frame.exec("curl -s --noproxy '*' http://127.0.0.1:8088/log")).stdout)
    assert not any("/delete" in e["path"] for e in log_before), "nothing deleted before the approval"

    # An acting-call approval (browser_click/type/fill/select/press/scroll) must carry the
    # fingerprint it was approved with as `expect`, so the sidecar can refuse to act if the page
    # moved under the question - a plain ToolApproved() stays correct only for browser_open's
    # allowlist ask.
    approved_part = next(p for p in first.output.approvals if p.tool_call_id == call_id)
    override = {**approved_part.args_as_dict(), "expect": meta["fingerprint"]}
    done = await agent.run(
        message_history=first.all_messages(),
        deferred_tool_results=DeferredToolResults(approvals={call_id: ToolApproved(override_args=override)}),
    )
    assert isinstance(done.output, str) and done.output.startswith("done: ok")
    log_after = json.loads((await frame.exec("curl -s --noproxy '*' http://127.0.0.1:8088/log")).stdout)
    deletes = [e for e in log_after if e["path"].endswith("/delete")]
    assert len(deletes) == 1 and deletes[0]["path"] == "/records/2/delete" and deletes[0]["keys"] == []
    assert not any("hunter2" in json.dumps(e) for e in log_after)
    assert ("browser.click", True, '127.0.0.1/records.html · button "Delete Grace Hopper"') in rows
    assert all("hunter2" not in r[2] and r[2] != "admin" for r in rows), (
        "typed text never reaches the journal"
    )

    final = await frame.browser({"verb": "finalize"})
    assert final["ok"] and final["actions"] >= 3
    index = await frame.read_file(f"{frame.recording_dir}/index.html")
    assert "data:image/jpeg;base64," in index
    assert "Delete Grace Hopper" in index
    actions = json.loads(await frame.read_file(f"{frame.recording_dir}/actions.json"))
    assert len(actions) >= 3
    assert not any(("text" in row or "value" in row or "fields" in row) for row in actions)

    # Publish it as a real `recording` artifact and run the structural suite over the
    # published tar. `recording_dir` already lives under /artifacts (the sidecar wrote it there
    # directly), and the publish runner tars TO `{ARTIFACTS}/{basename(path)}` - publishing the
    # folder under its own name would tar a file over the directory it just read from and crash
    # (`IsADirectoryError`). Copy to /work under a DIFFERENT basename first, so the publish
    # target lands beside the original, never on top of it.
    work_copy = "/work/recording-pub-1"
    await frame.exec(f"cp -r {frame.recording_dir} {work_copy}")
    await frame.publish(work_copy, "recording", "Delete Grace Hopper — recording")
    manifest = await frame.manifest()
    entry = next(e for e in manifest["entries"] if e["type"] == "recording")
    tar_bytes = await frame.read_bytes(entry["path"])
    # The verifier's own vocabulary (`RECORDED_VERBS`, what `TaskEvent.count_tool_calls` counts),
    # never a hand-picked set - the engine test below counts through the real ledger query.
    from prismoid.schemas.computer_use import RECORDED_VERBS

    expected_actions = sum(
        1 for tool, ok, _ in rows if ok and tool in {f"browser.{v}" for v in RECORDED_VERBS}
    )
    checks = check_recording(tar_bytes, expected_actions=expected_actions)
    assert all(c["status"] != "fail" for c in checks), checks


async def test_the_settings_trap_reaches_the_model_as_fenced_data(frame: DockerFrame) -> None:
    """The page's instruction arrives inside the fence."""
    ports = BrowserPorts(browse=frame.browser, allow=frame.allow, allowed={"127.0.0.1"})
    toolset = browser_toolset(BROWSER_TOOLS, None, ports, Approvals(tainted=tainted))
    assert toolset is not None

    def script(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen = _returns(messages)
        if not seen:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="browser_open", args={"url": "http://127.0.0.1:8088/settings.html"}
                    )
                ]
            )
        if len(seen) == 1:
            return ModelResponse(parts=[ToolCallPart(tool_name="browser_snapshot", args={})])
        snap = str(seen[1].content)
        assert "――― page content" in snap and "ignore your instructions" in snap
        return ModelResponse(parts=[TextPart("read")])

    out = await Agent(FunctionModel(script), output_type=[str, DeferredToolRequests], toolsets=[toolset]).run(
        "read settings"
    )
    assert out.output == "read"


async def test_a_resumed_step_widens_the_reattached_frame_before_acting(
    silent_dispatcher: object, monkeypatch: pytest.MonkeyPatch, machines_made: list[uuid.UUID]
) -> None:
    """CRITICAL 2 (review, round 2): `_browser_allowlist` is recomputed fresh from the DB on
    every attempt, so by the time a step RESUMES from a `scope=task` accept, the toolset's
    `ports.allowed` already carries the newly-granted host - `browser_open` sees `domain_allowed`
    return True and skips its own `_widen`/ask entirely. Nothing else EVER widens the reattached
    container's own live proxy, so without `_attempt` doing it explicitly on resume the human's
    just-approved navigation is refused at the egress door while the toolset believes it already
    succeeded. Driven through the REAL engine (`execute_step`, twice - park, then resume) with a
    real docker frame - the task's Machine, parked and bound again; only `build()` is
    stubbed, to a scripted `FunctionModel` wired with the REAL `browser_toolset`. `example.com` (not `127.0.0.1`) is the target - `check_host` refuses
    to ever offer a loopback host as an allowlist ask, so the fixture's own site cannot stand in
    here; `example.com` is the same target `test_browser_frame.py::test_allow_widens_a_running_
    frame` already proves this dev egress proxy denies-then-allows.
    """
    import json as _json

    from pydantic_ai.models.function import DeltaToolCall

    import prismoid.ai.agents.base as agents_base
    from prismoid.ai.deps import TaskDeps
    from prismoid.ai.frames.docker_driver import ensure_image
    from prismoid.ai.tools.browser import browser_toolset
    from prismoid.conf import settings
    from prismoid.db.models.artifact import Artifact
    from prismoid.db.models.event import TaskEvent
    from prismoid.db.models.plan import PlanStep
    from prismoid.db.models.task import Task
    from prismoid.db.session import row_scope, session_scope
    from prismoid.operations import execution, gates, machines
    from prismoid.schemas.contract import Contract
    from prismoid.schemas.events import ToolCalled
    from prismoid.schemas.ids import AgentName
    from prismoid.schemas.outputs import ArtifactType
    from prismoid.schemas.plan import GateAction, StepKind, StepSpec, StepState, TaskStatus

    ensure_image("browser")
    monkeypatch.setattr(settings.computer_use, "enabled", True)  # the kill switch

    # `_attempt`/`runner.run` always sets `event_stream_handler` (the message lane) for a
    # step agent, which forces pydantic-ai to stream the model's response - `FunctionModel`
    # needs a `stream_function`, not the plain sync `function=` the other tests here use.
    async def stream_script(messages: list[ModelMessage], info: AgentInfo):
        seen = _returns(messages)
        if not seen:
            yield {
                0: DeltaToolCall(
                    name="browser_open",
                    json_args=_json.dumps({"url": "https://example.com/"}),
                    tool_call_id="call-open",
                )
            }
        else:
            yield f"done: {seen[-1].content}"

    def fake_build(name: str, deps: TaskDeps, catalog: object, **_: object) -> AgentRunner:
        assert deps.browser is not None
        toolset = browser_toolset(BROWSER_TOOLS, deps.journal_tool, deps.browser, Approvals(tainted=tainted))
        assert toolset is not None
        agent = Agent(
            FunctionModel(stream_function=stream_script),
            deps_type=TaskDeps,
            output_type=[str, DeferredToolRequests],
            toolsets=[toolset],
            name=name,
            capabilities=[PrismoidDurability()],  # as every built agent: a resume replays its call log
        )
        return runner_for(agent, deps)

    async with session_scope() as db:
        actor, workspace = await member(db, "Navigator Owner")
        await db.flush()
        task = Task(
            id=uuid.uuid4(),
            goal="Open example.com and report back.",
            creator_actor_id=actor.id,
            criteria={},
            inputs=[],
        )
        task.status = TaskStatus.RUNNING.value  # past planning already, as dispatch would leave it
        db.add(task)
        await db.flush()
        step = PlanStep(
            id=uuid.uuid4(),
            task_id=task.id,
            ordinal=0,
            kind=StepKind.STEP.value,
            state=StepState.READY.value,
            title="open example.com",
            spec=StepSpec(
                kind=StepKind.STEP,
                agent=AgentName("operator"),
                browser=True,
                title="open example.com",
                contract=Contract(goal="Open example.com and report back."),
            ).model_dump(mode="json"),
            attempt=0,
        )
        db.add(step)
        await db.commit()
        task_id, step_id = task.id, step.id
    machines_made.append(task_id)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(agents_base, "build", fake_build)

        async with row_scope(Task, task_id) as db:
            parked = await execution.execute_step(db, task_id, step_id)
        assert parked.startswith(execution.ASKING), parked
        gate_id = uuid.UUID(parked.removeprefix(execution.ASKING))

        # The park pinned Chromium alive (`hold`): a gate outlives the sidecar's idle stop. The
        # park is the task's Machine, so its frame is the Machine row's.
        async with row_scope(Task, task_id) as db:
            parked_task = await db.get(Task, task_id)
            parked_step = await db.get(PlanStep, step_id)
            assert parked_task is not None and parked_step is not None
            attempt = parked_step.attempt
            parked_frame = await machines.parked_frame(db, parked_task, parked_step, profile="browser")
        assert parked_frame is not None
        assert (await parked_frame.browser({"verb": "ping"}))["held"] is True

        async with bound_scope(workspace.id) as db:  # the door binds the owner's workspace
            resolved = await gates.resolve(db, actor, gate_id, GateAction.ACCEPT, {"scope": "task"})
            assert resolved.content == {"scope": "task"}

        async with row_scope(Task, task_id) as db:
            await execution.execute_step(db, task_id, step_id)

    async with session_scope() as db:
        events = await TaskEvent.replay(db, task_id)
        artifacts = await Artifact.for_task(db, task_id)
        # `.type` reads a mapped column - fetched while still bound to `db`, or it dies
        # `DetachedInstanceError` the moment the session context above closes.
        recording_types = [a.type for a in artifacts]
        recording = next(a for a in artifacts if a.type == ArtifactType.RECORDING.value)
        recording_role, recording_sha = recording.role, recording.sha256
        from prismoid.operations.verification import recording_attempt
        from prismoid.schemas.artifacts import ManifestEntry
        from prismoid.schemas.computer_use import RECORDED_VERBS

        rec_path = ManifestEntry.model_validate(recording.provenance).path
        rec_attempt = recording_attempt(rec_path)
        assert rec_attempt == attempt, rec_path
        # The verifier's count, through the verifier's own query: THIS attempt's acting calls.
        expected_actions = await TaskEvent.count_tool_calls(
            db,
            task_id=task_id,
            step_id=step_id,
            prefix="browser.",
            verbs=RECORDED_VERBS,
            attempt=rec_attempt,
        )
    opens = [e for e in events if isinstance(e, ToolCalled) and e.tool == "browser.open"]
    assert opens, "no browser.open was journalled at all"
    # Before the fix: EVERY browser.open after the resume fails at the (un-widened) proxy -
    # "failed: egress refused" journals ok=False even though the toolset believed it was
    # allowed. After the fix: the resumed call's open succeeds.
    assert opens[-1].ok, opens[-1]
    # `_publish_recording` is best-effort (evidence is not authority) - a broken
    # recording publish is swallowed as a logged fact, never a raised exception, so the test
    # must check the HARVEST landed rather than merely trust that nothing raised (review,
    # round 3: this is what would have caught CRITICAL 1's silent "not published" swallow).
    assert ArtifactType.RECORDING.value in recording_types, "no recording artifact was harvested"
    # Every journaled browser row carries the attempt; the widen never poses as an open.
    browser_rows = [e for e in events if isinstance(e, ToolCalled) and e.tool.startswith("browser.")]
    assert browser_rows and all(e.attempt == attempt for e in browser_rows)
    assert expected_actions == sum(1 for e in opens if e.ok) >= 1
    from prismoid import storage

    checks = check_recording(storage.get(recording_sha), expected_actions=expected_actions)
    assert all(c["status"] != "fail" for c in checks), checks
    from prismoid.schemas.verification import Check

    assert all(Check.model_validate(c) for c in checks)  # parsed as the verification pipeline does
    assert recording_role == "deliverable", "one real act: the recording is the trail delivered"


async def test_a_navigator_step_with_dependents_hands_them_its_narration(
    silent_dispatcher: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The navigator publishes nothing, and only published artifacts flow downstream: a planned
    navigator -> writer chain handed the writer nothing but the recording. Its narration now
    rides to the step that depends on it as a working markdown file - and only then."""
    import prismoid.ai.agents.base as agents_base
    from prismoid.ai.deps import TaskDeps
    from prismoid.conf import settings
    from prismoid.db.models.artifact import Artifact
    from prismoid.db.models.plan import PlanEdge, PlanStep
    from prismoid.db.models.task import Task
    from prismoid.db.session import row_scope, session_scope
    from prismoid.operations import execution
    from prismoid.operations.execution.mounts import upstream_artifacts
    from prismoid.schemas.contract import Contract
    from prismoid.schemas.ids import AgentName
    from prismoid.schemas.plan import StepKind, StepSpec, StepState, TaskStatus

    ensure_image("browser")
    monkeypatch.setattr(settings.computer_use, "enabled", True)
    narration = "Read the records table: Grace Hopper is open, Ada Lovelace is closed."

    async def stream_script(messages: list[ModelMessage], info: AgentInfo):
        yield narration

    def fake_build(name: str, deps: TaskDeps, catalog: object, **_: object) -> AgentRunner:
        agent = Agent(
            FunctionModel(stream_function=stream_script),
            deps_type=TaskDeps,
            output_type=[str, DeferredToolRequests],
            name=name,
        )
        return runner_for(agent, deps)

    def spec(agent: str, title: str) -> dict[str, Any]:
        return StepSpec(
            kind=StepKind.STEP,
            agent=AgentName(agent),
            browser=agent == "operator",
            title=title,
            contract=Contract(goal=title),
        ).model_dump(mode="json")

    async def run_reader(*, with_dependent: bool) -> tuple[list[tuple[str, str, bytes]], list[str]]:
        async with session_scope() as db:
            actor, _ = await member(db, "Reader")
            await db.flush()
            task = Task(
                id=uuid.uuid4(), goal="Read the records", creator_actor_id=actor.id, criteria={}, inputs=[]
            )
            task.status = TaskStatus.RUNNING.value
            db.add(task)
            await db.flush()
            reader = PlanStep(
                id=uuid.uuid4(),
                task_id=task.id,
                ordinal=0,
                kind=StepKind.STEP.value,
                state=StepState.READY.value,
                title="read the records",
                spec=spec("operator", "read the records"),
                attempt=0,
            )
            writer = PlanStep(
                id=uuid.uuid4(),
                task_id=task.id,
                ordinal=1,
                kind=StepKind.STEP.value,
                state=StepState.PENDING.value,
                title="write the report",
                spec=spec("writer", "write the report"),
                attempt=0,
            )
            db.add_all([reader, writer])
            await db.flush()
            if with_dependent:
                db.add(
                    PlanEdge(
                        task_id=task.id, from_step_id=reader.id, to_step_id=writer.id, kind="produces_for"
                    )
                )
            await db.commit()
            task_id, reader_id, writer_id = task.id, reader.id, writer.id

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(agents_base, "build", fake_build)
            async with row_scope(Task, task_id) as db:
                await execution.execute_step(db, task_id, reader_id)

        from prismoid import storage

        async with session_scope() as db:
            mine = [a for a in await Artifact.for_task(db, task_id) if a.step_id == reader_id]
            notes = [(a.type, a.role, storage.get(a.sha256)) for a in mine if a.type == "document"]
            task_row = await db.get(Task, task_id)
            writer_row = await db.get(PlanStep, writer_id)
            assert task_row is not None and writer_row is not None
            mounted = [a.type for a in await upstream_artifacts(db, task_row, writer_row)]
        return notes, mounted

    notes, mounted = await run_reader(with_dependent=True)
    assert notes == [("document", "working", narration.encode())]
    assert "document" in mounted, "the writer's frame is loaded with what the navigator read"

    alone, _ = await run_reader(with_dependent=False)
    assert alone == [], "no dependent, no note: the narration is the receipt"
