# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A grant revoked under a running step, and the real helper's journal: a revoke reaches the frame
before the next tool, and a command cut off is never run again. The rig
is `tests/support/rigs/device_recovery_rig.py`."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import pwd
import shutil
import tempfile
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, cast

import pytest
import uvicorn
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, DeltaToolCalls

from prismoid.ai.frames import new_frame
from prismoid.conf import settings
from prismoid.db.models.actor import Actor
from prismoid.db.models.device_grant import DeviceGrant
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope
from prismoid.operations import device_grants, execution, machines
from prismoid.schemas.device_grants import GrantKind, GrantMode, folder_key, mint_grant_name
from prismoid.schemas.events import (
    ArtifactSnapshot,
    StepPrompt,
    ToolCalled,
)
from prismoid.schemas.plan import StepState, TaskStatus
from tests.support.dispatchers import RecordingDispatcher
from tests.support.fakes.fake_frames import DEVICE, FakeFrame, NoneSleepFakeFrame
from tests.support.markers import darwin_only
from tests.support.rigs.device_recovery_rig import (
    APPEND,
    NOTE,
    PUBLISH,
    WRITE,
    Stream,
    beside_a_refusal,
    call,
    device_step,
    gates_of,
    recovery_events,
    recovery_ran,
    recovery_row,
    recovery_run,
    recovery_told,
    refuse_reads,
    resume_and_wake,
    working,
)
from tests.support.rigs.device_recovery_rig import private_database as private_database
from tests.support.rigs.device_recovery_rig import real_agent as real_agent

pytestmark = pytest.mark.integration


def _scripted(requests: list[list[ModelMessage]], turns: list[Any]) -> Stream:
    """One turn per request: a tool call, the closing text, or a coroutine function that does what
    a person does meanwhile and answers with either."""

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        requests.append(list(messages))
        turn: Any = turns[len(requests) - 1]
        said: Any = await cast(Any, turn)() if callable(turn) else turn
        if isinstance(said, str):
            yield said
        else:
            name, args = said
            yield call(name, args, f"c{len(requests)}")

    return stream


async def _grant_folder(task_id: uuid.UUID, ada_id: uuid.UUID, path: str) -> uuid.UUID:
    """A read-only row on the task: nothing of it is read from a disk here."""
    async with row_scope(Task, task_id) as s:
        row = DeviceGrant(
            task_id=task_id,
            kind=GrantKind.FOLDER,
            path=path,
            path_key=folder_key(path),
            mode=GrantMode.RO,
            name=mint_grant_name(path, []),
            created_by_actor_id=ada_id,
        )
        s.add(row)
        await s.commit()
        return row.id


async def _revoke(ada_id: uuid.UUID, task_id: uuid.UUID, grant_id: uuid.UUID) -> None:
    async with row_scope(Task, task_id) as s:
        actor = await s.get(Actor, ada_id)
        assert actor is not None
        await device_grants.revoke_grant(s, actor, grant_id)


def _live_frame(fake_frames: Any, task_id: uuid.UUID) -> Any:
    return fake_frames.world[f"device-{task_id}"]


async def test_a_revoke_reaches_the_frame_before_the_next_tool(
    fake_frames: Any,
    silent_dispatcher: None,
    monkeypatch: pytest.MonkeyPatch,
    real_agent: Callable[[Stream], None],
) -> None:
    """A revoke between two tools of one run reaches the frame before the second runs, and only
    then: tools with nothing changed ask Postgres and leave the helper alone."""
    requests: list[list[ModelMessage]] = []
    task_id, step_id, ada_id, _ = await device_step()
    grant_id = await _grant_folder(task_id, ada_id, "/Users/ada/notes")
    held: list[list[str]] = []
    reads: list[uuid.UUID] = []
    real_read = device_grants.grants_for

    async def counted(session: Any, of: uuid.UUID) -> Any:
        reads.append(of)
        return await real_read(session, of)

    monkeypatch.setattr(device_grants, "grants_for", counted)

    def note() -> None:
        told = _live_frame(fake_frames, task_id).granted
        held.append([g.path for g in told[0]])

    async def revoked() -> tuple[str, dict[str, Any]]:
        note()
        await _revoke(ada_id, task_id, grant_id)
        return ("shell", {"command": "ls"})

    async def after() -> tuple[str, dict[str, Any]]:
        note()
        return WRITE

    real_agent(
        _scripted(requests, [("shell", {"command": "pwd"}), revoked, after, PUBLISH, "done"]),
    )

    await recovery_run(task_id, step_id)

    assert held == [["/Users/ada/notes"], []], "the second tool ran on the grants as they are now"
    assert len(reads) == 3, "the acquire's, the attempt's first compare, the revoke's: no other tool"
    done = await recovery_row(task_id, PlanStep, step_id)
    assert done.step_state is StepState.DONE and done.attempt == 1


async def test_a_push_that_fails_never_runs_the_tool(
    fake_frames: Any,
    recording_dispatcher: RecordingDispatcher,
    real_agent: Callable[[Stream], None],
) -> None:
    """The helper is gone as the revoke comes: the next tool does not run on the old grant. The
    step waits for its device, as for any tool the helper did not answer."""
    requests: list[list[ModelMessage]] = []
    task_id, step_id, ada_id, _ = await device_step()
    grant_id = await _grant_folder(task_id, ada_id, "/Users/ada/notes")

    async def revoked() -> tuple[str, dict[str, Any]]:
        await _revoke(ada_id, task_id, grant_id)
        fake_frames.unreachable = True
        return ("shell", {"command": "rm -rf /grants/notes"})

    real_agent(_scripted(requests, [("shell", {"command": "pwd"}), revoked, "done"]))

    outcome = await recovery_run(task_id, step_id)

    fake_frames.unreachable = False
    assert outcome.startswith(execution.ASKING), outcome
    assert recovery_ran(fake_frames, "rm -rf /grants/notes") == 0
    (gate,) = await gates_of(task_id)
    assert gate.spec["reason"].startswith("device_away:interrupted\n")


async def test_only_a_machine_on_the_device_is_fenced(
    fake_frames: Any,
    silent_dispatcher: None,
    monkeypatch: pytest.MonkeyPatch,
    real_agent: Callable[[Stream], None],
) -> None:
    """A cloud frame takes no grants, so nothing is asked; a seat's throwaway frame holds none of
    the task's, so it is never told them."""
    cloud: Any = FakeFrame("t")
    assert machines.grant_fence(uuid.uuid4(), cloud) is None
    device: Any = NoneSleepFakeFrame("t")
    assert machines.grant_fence(uuid.uuid4(), device) is not None
    assert machines.grant_fence(uuid.uuid4(), device, seat=True) is None

    reads: list[uuid.UUID] = []
    real_read = device_grants.grants_for

    async def counted(session: Any, of: uuid.UUID) -> Any:
        reads.append(of)
        return await real_read(session, of)

    monkeypatch.setattr(device_grants, "grants_for", counted)
    requests: list[list[ModelMessage]] = []
    task_id, step_id, _, _ = await device_step()
    real_agent(_scripted(requests, [WRITE, PUBLISH, "done"]))
    async with row_scope(Task, task_id) as session:
        await execution.execute_step(session, task_id, step_id, seat=True)
    assert (await recovery_row(task_id, PlanStep, step_id)).step_state is StepState.DONE
    assert reads == [], "a seat's run reads no grant of the task's"


def _helper_home(prefix: str = "pr") -> Path:
    """A short, resolved folder: a socket path must fit sun_path, and `/tmp` is `/private/tmp`."""
    return Path(os.path.realpath(tempfile.mkdtemp(prefix=prefix, dir="/tmp")))


class _Helper:
    """`prismoid device helper` in-process on its Unix socket (the conformance suite's own way): the
    same app, a real Seatbelt fence, a real journal. `stop` is the helper's life ending - what it
    left running dies with it - and `start` again is a new helper life on the same home."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.server: uvicorn.Server | None = None
        self.serving: asyncio.Task[None] | None = None

    async def start(self) -> None:
        from prismoid.runner.device_door import create_app

        app = create_app(self.home, device_name=lambda: "Recovery Mac")
        config = uvicorn.Config(
            app, uds=str(self.home / "run" / "helper.sock"), log_level="critical", timeout_graceful_shutdown=1
        )
        self.server = uvicorn.Server(config)
        self.serving = asyncio.create_task(self.server.serve())
        while not self.server.started:
            if self.serving.done():
                await self.serving
            await asyncio.sleep(0.02)

    async def stop(self) -> None:
        assert self.server is not None and self.serving is not None
        self.server.should_exit = True
        await self.serving


@darwin_only
async def test_kill_mid_command_never_reruns(
    monkeypatch: pytest.MonkeyPatch,
    recording_dispatcher: RecordingDispatcher,
    real_agent: Callable[[Stream], None],
) -> None:
    """The plan's highest-risk test: `prismoid up` is killed while the step's command runs
    in the user's folder - the worker dies inside the command and the helper's life ends under it -
    and both come back. The recovered step parks; the folder holds exactly the one line the command
    wrote, however long nobody comes; the person's resume is told what was cut off, and the line is
    still one. The worker's death is the recovery tests' simulation (the step's coroutine dies, the
    durable record is what it committed); the helper is real."""
    home = _helper_home()
    monkeypatch.setattr(settings.local, "home", home)
    monkeypatch.setattr(settings.worker, "instance", f"b6-{uuid.uuid4().hex[:8]}")
    helper = _Helper(home)
    await helper.start()
    task_id: uuid.UUID | None = None
    try:
        task_id, step_id, _, bob_id = await device_step()
        log = home / "tasks" / str(task_id) / "work" / "log.txt"
        requests: list[list[ModelMessage]] = []
        real_agent(working(requests, dies=False, command=APPEND.replace("/work", str(log.parent))))

        worker = asyncio.create_task(recovery_run(task_id, step_id))
        for _ in range(400):  # the command has written its line and sleeps on
            if log.exists() and log.read_text():
                break
            await asyncio.sleep(0.05)
        assert log.read_text() == "line\n"
        worker.cancel()  # the worker dies mid-command
        with pytest.raises((asyncio.CancelledError, BaseExceptionGroup)):
            await worker
        await helper.stop()  # ... and the helper's life ends under it: the command is killed
        await helper.start()  # `prismoid up` again

        outcome = await recovery_run(task_id, step_id)  # the recovered workflow

        assert log.read_text() == "line\n", "the recovered step ran nothing again"
        assert outcome.startswith(execution.ASKING), outcome
        (gate,) = await gates_of(task_id)
        assert gate.spec["reason"].startswith("device_away:interrupted\n")
        assert gate.spec["title"] == "Waiting for Recovery Mac"
        assert "Cut off: `echo line >> /work/log.txt && sleep 30`" in gate.spec["reason"]
        assert (await recovery_row(task_id, Task, task_id)).state is TaskStatus.PAUSED
        for _ in range(3):  # nobody comes: a recv timeout, a restart - each re-parks, nothing runs
            assert await recovery_run(task_id, step_id) == outcome
            await asyncio.sleep(0.2)
        assert log.read_text() == "line\n", "nothing ran again before a person resumed"
        assert len(requests) == 1

        await resume_and_wake(recording_dispatcher, bob_id, task_id, step_id)
        assert (await recovery_run(task_id, step_id)).strip() != ""

        # The record keeps the virtual roots; the agent reads them spelled as the device has them.
        assert NOTE in [e.prompt for e in await recovery_events(task_id, StepPrompt)][-1]
        assert NOTE.replace("/work/log.txt", str(log)) in recovery_told(requests[1])
        assert log.read_text() == "line\n", "the resumed attempt was told, and did not run it again"
        done = await recovery_row(task_id, PlanStep, step_id)
        assert done.step_state is StepState.DONE and done.model_attempts() == 1
    finally:
        if task_id is not None:  # its cache folder under ~/Library/Caches/Prismoid goes with the frame
            with contextlib.suppress(Exception):
                await new_frame(str(task_id), driver=DEVICE, name=f"device-{task_id}").destroy()
        await helper.stop()
        shutil.rmtree(home, ignore_errors=True)


@darwin_only
async def test_a_refused_sibling_can_no_longer_leave_a_command_running(
    monkeypatch: pytest.MonkeyPatch,
    recording_dispatcher: RecordingDispatcher,
    real_agent: Callable[[Stream], None],
) -> None:
    """The review's door, closed, on the real helper. It was a parallel batch whose refused sibling
    ended the attempt while `shell` ran (a Starlette handler is not cancelled when its client goes,
    so the command ran on in the person's folder and the next rung had to park on it). Frame tools
    now run one at a time: the command finishes before its sibling is refused, the refusal costs
    only that attempt, and the next one finds nothing running - no park, the line written once.
    (This replaced the test that proved the park; the park itself is still proven for a wall-clock
    stop and a helper that went away.)"""
    home = _helper_home()
    monkeypatch.setattr(settings.local, "home", home)
    monkeypatch.setattr(settings.worker, "instance", f"b6-{uuid.uuid4().hex[:8]}")
    helper = _Helper(home)
    await helper.start()
    task_id: uuid.UUID | None = None
    try:
        task_id, step_id, _, _ = await device_step()
        log = home / "tasks" / str(task_id) / "work" / "log.txt"

        async def command_ran() -> None:
            assert log.exists() and log.read_text() == "line\n", "the command finished before the refusal"

        refuse_reads(monkeypatch, once=command_ran)
        requests: list[list[ModelMessage]] = []
        command = APPEND.replace("/work", str(log.parent)).replace(" && sleep 30", "")
        real_agent(beside_a_refusal(requests, command=command))

        outcome = await recovery_run(task_id, step_id)

        assert not outcome.startswith(execution.ASKING), outcome
        assert log.read_text() == "line\n", "the command ran once, to its end"
        assert await gates_of(task_id) == [], "nothing was left running to park on"
        assert (await recovery_row(task_id, PlanStep, step_id)).step_state is StepState.DONE
    finally:
        if task_id is not None:  # its cache folder under ~/Library/Caches/Prismoid goes with the frame
            with contextlib.suppress(Exception):
                await new_frame(str(task_id), driver=DEVICE, name=f"device-{task_id}").destroy()
        await helper.stop()
        shutil.rmtree(home, ignore_errors=True)


@darwin_only
async def test_a_revoked_folder_refuses_the_next_write_in_the_same_step(
    monkeypatch: pytest.MonkeyPatch,
    silent_dispatcher: None,
    real_agent: Callable[[Stream], None],
) -> None:
    """On the real helper and the real fence: one step writes in a granted folder, a
    person revokes it, and the same run's next write is refused by Seatbelt, by the file door, and
    by the root's own name. Nothing parks and no attempt is spent."""
    home = _helper_home()
    monkeypatch.setattr(settings.local, "home", home)
    monkeypatch.setattr(settings.worker, "instance", f"i2-{uuid.uuid4().hex[:8]}")
    folder = Path(tempfile.mkdtemp(prefix="prismoid-i2-", dir=pwd.getpwuid(os.getuid()).pw_dir)).resolve()  # noqa: ASYNC240 - test setup
    helper = _Helper(home)
    await helper.start()
    task_id: uuid.UUID | None = None
    try:
        task_id, step_id, ada_id, _ = await device_step()
        async with row_scope(Task, task_id) as s:
            actor = await s.get(Actor, ada_id)
            assert actor is not None
            granted = await device_grants.add_grant(
                s, actor, task_id, kind=GrantKind.FOLDER, path=str(folder)
            )
            grant_id, name = granted.id, granted.name
        assert name is not None

        async def revoked() -> tuple[str, dict[str, Any]]:
            await _revoke(ada_id, task_id, grant_id)
            return ("shell", {"command": f"echo two > {folder}/after-shell.txt"})

        requests: list[list[ModelMessage]] = []
        real_agent(
            _scripted(
                requests,
                [
                    ("shell", {"command": f"echo one > {folder}/before.txt"}),
                    revoked,
                    ("write_file", {"path": f"{folder}/after-door.txt", "content": "three"}),
                    ("shell", {"command": f"echo four > {name}/after-virtual.txt"}),
                    WRITE,
                    PUBLISH,
                    "done",
                ],
            )
        )

        await recovery_run(task_id, step_id)

        assert (folder / "before.txt").read_text() == "one\n", "the grant held until the revoke"
        for refused in ("after-shell.txt", "after-door.txt", "after-virtual.txt"):
            assert not (folder / refused).exists(), f"{refused} reached the revoked folder"
        said = "\n".join(str(p) for r in requests[2:] for m in r for p in m.parts)
        assert "Operation not permitted" in said, "the fence refused it, and the model heard"
        done = await recovery_row(task_id, PlanStep, step_id)
        assert done.step_state is StepState.DONE and done.attempt == 1, "the same step, the same attempt"
        assert not await gates_of(task_id)
    finally:
        if task_id is not None:  # its cache folder under ~/Library/Caches/Prismoid goes with the frame
            with contextlib.suppress(Exception):
                await new_frame(str(task_id), driver=DEVICE, name=f"device-{task_id}").destroy()
        await helper.stop()
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(folder, ignore_errors=True)


@darwin_only
async def test_the_journal_says_virtual_paths_whatever_the_model_typed(
    monkeypatch: pytest.MonkeyPatch,
    silent_dispatcher: None,
    real_agent: Callable[[Stream], None],
) -> None:
    """The model is told real paths, so a tool's args hold them. Through the real tools, the real
    helper and a home with a space in it (the root goes by an alias): the stored `tool.called` rows
    and the durable snapshot say `/work`, and a path off the frame is not said at all."""
    home = _helper_home("pr sp")
    monkeypatch.setattr(settings.local, "home", home)
    monkeypatch.setattr(settings.worker, "instance", f"lbl-{uuid.uuid4().hex[:8]}")
    helper = _Helper(home)
    await helper.start()
    task_id: uuid.UUID | None = None
    try:
        task_id, step_id, _, _ = await device_step()
        memo = f"{home}/tasks/{task_id}/work/memo.md"
        escaped = memo.replace(" ", "\\ ")
        requests: list[list[ModelMessage]] = []
        real_agent(
            _scripted(
                requests,
                [
                    ("write_file", {"path": memo, "content": "# memo\n" * 3000}),
                    ("read_file", {"path": memo}),
                    ("shell", {"command": f"wc -l {escaped} '{memo}'"}),
                    ("write_file", {"path": str(Path.home() / ".ssh" / "prismoid-test"), "content": "x"}),
                    ("publish", {"path": memo, "type": "document", "title": "Memo"}),
                    "done",
                ],
            )
        )

        await recovery_run(task_id, step_id)

        assert (await recovery_row(task_id, PlanStep, step_id)).step_state is StepState.DONE
        called = [(e.tool, e.ok, e.summary) for e in await recovery_events(task_id, ToolCalled)]
        assert called == [
            ("frame.write_file", True, "/work/memo.md"),
            ("frame.read_file", True, "/work/memo.md"),
            ("frame.shell", True, "wc -l /work/memo.md '/work/memo.md'"),
            ("frame.write_file", False, "that path"),
            ("frame.publish", True, "document:/work/memo.md"),
        ]
        assert [e.path for e in await recovery_events(task_id, ArtifactSnapshot)] == ["/work/memo.md"]
        dumped = json.dumps([e.model_dump(mode="json") for e in await recovery_events(task_id, ToolCalled)])
        assert str(home) not in dumped and ".ssh" not in dumped
    finally:
        if task_id is not None:  # its cache folder under ~/Library/Caches/Prismoid goes with the frame
            with contextlib.suppress(Exception):
                await new_frame(str(task_id), driver=DEVICE, name=f"device-{task_id}").destroy()
        await helper.stop()
        shutil.rmtree(home, ignore_errors=True)
