# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The memory fence. A task in a Project is shared work - it never reads anyone's
private memory into its context, and never mints any from the room's conversation.

Before the fence a Project task loaded its CREATOR's private USER memories (and the creator's
personal-bot lane) and surfaced previews of them to every member. Now the pack for a Project task
carries the Project's own lanes only, and both memory extractions stand down.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, MutableMapping
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.api.app import create_app
from prismoid.api.hub import hub
from prismoid.db.base import utcnow
from prismoid.db.models import tenant
from prismoid.db.models.actor import Actor
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.memory import Memory, MemoryKind, MemoryScope
from prismoid.db.models.project import Project
from prismoid.db.models.project_member import ProjectMember
from prismoid.db.models.task import Task
from prismoid.db.models.workspace import WorkspaceMember, WorkspaceRole
from prismoid.db.session import session_scope
from prismoid.operations import auth as auth_ops
from prismoid.operations import bots as bot_ops
from prismoid.operations import context as context_ops
from prismoid.operations import memory as memory_ops
from prismoid.operations import projects as project_ops
from prismoid.operations import steering
from prismoid.operations import tasks as task_ops
from prismoid.operations.execution import run as ex_run
from prismoid.schemas.bots import BotCase, BotSource
from prismoid.schemas.context import ContextItem, ContextLane, ContextPack
from prismoid.schemas.events import BotMemoryWritten, MemoryStub, MemoryWritten, StepPrompt, TaskDelivered
from prismoid.schemas.ids import MemoryId, TaskId
from prismoid.schemas.outputs import MemoryExtract
from prismoid.schemas.plan import TaskStatus
from tests.support.cast import member

pytestmark = pytest.mark.integration

CODEWORD = "My codeword is PINEAPPLE-42"


def _source(name: str) -> BotSource:
    return BotSource(
        job="Reviews vendor contracts",
        use_when=("an MSA needs a close read",),
        not_for=("legal advice",),
        instructions="Review every clause closely.",
        cases=(BotCase(goal="Review this MSA", expect=name),),
    )


async def _room(s: AsyncSession) -> tuple[Actor, Actor, Project, Task, Task]:
    """Ada owns a Project and holds a private codeword; Bob edits there. Returns Ada, Bob, the
    Project, Ada's Project task, and Ada's personal (Project-less) task."""
    ada, workspace = await member(s, "Ada")
    bob, _ = await member(s, "Bob", role=WorkspaceRole.MEMBER, workspace=workspace)
    project = (await project_ops.create(s, ada, name=f"fence-{uuid.uuid4().hex[:6]}")).project
    s.add(ProjectMember(project_id=project.id, actor_id=bob.id, role="editor", added_by=ada.id))
    await s.flush()
    await memory_ops.create(s, ada, scope=MemoryScope.USER, kind=MemoryKind.FACT, text=CODEWORD)
    await memory_ops.create(
        s,
        ada,
        scope=MemoryScope.PROJECT,
        kind=MemoryKind.NOTE,
        text="This team ships UK English.",
        project_id=project.id,
    )
    project_task = await task_ops.create_task(s, ada, goal="compare three trackers", project_id=project.id)
    personal_task = await task_ops.create_task(s, ada, goal="compare three trackers")
    return ada, bob, project, project_task, personal_task


def _lane_names(pack: ContextPack) -> list[str]:
    return [lane.lane for lane in pack.lanes]


def _texts(pack: ContextPack) -> list[str]:
    return [item.text for lane in pack.lanes for item in lane.items]


async def test_a_project_task_loads_no_user_lane(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        _ada, _bob, _project, project_task, _personal = await _room(s)

        pack = await context_ops.assemble(s, project_task)

        assert pack is not None
        assert "project" in _lane_names(pack)
        assert "user" not in _lane_names(pack)
        assert not any("PINEAPPLE" in t for t in _texts(pack)), "no lane item carries the private memory"


async def test_a_personal_task_still_loads_it(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        _ada, _bob, _project, _project_task, personal = await _room(s)

        pack = await context_ops.assemble(s, personal)

        assert pack is not None
        lanes = {lane.lane: lane for lane in pack.lanes}
        assert "user" in lanes
        assert any("PINEAPPLE-42" in item.text for item in lanes["user"].items)
        assert "project" not in lanes, "a Project-less task has no Project lane"


async def test_a_personal_bots_lane_stays_out_of_a_project_task(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        ada, _bob, project, project_task, personal = await _room(s)

        # A personal bot: its BOT memory rides the owner (user) carrier.
        await bot_ops.push(s, actor=ada, name="reviewer", source=_source("reviewer"))
        await memory_ops.create(
            s,
            ada,
            scope=MemoryScope.BOT,
            kind=MemoryKind.FACT,
            text="Ada's private vendor list lives in her drive.",
            bot_name="reviewer",
        )
        # A Project bot: its BOT memory rides the Project carrier - shared by design.
        await bot_ops.push(s, actor=ada, name="scribe", source=_source("scribe"), project_id=project.id)
        await memory_ops.create(
            s,
            ada,
            scope=MemoryScope.BOT,
            kind=MemoryKind.PREFERENCE,
            text="Dana wants auto-renewal clauses flagged.",
            bot_name="scribe",
            project_id=project.id,
        )

        # Control: the personal bot on a personal task still has its lane.
        personal.direct_bot = "reviewer"
        await s.flush()
        pack = await context_ops.assemble(s, personal)
        assert pack is not None and "bot" in _lane_names(pack)

        # The fence: the same personal bot pinned on a Project task adds no bot lane.
        project_task.direct_bot = "reviewer"
        await s.flush()
        pack = await context_ops.assemble(s, project_task)
        assert pack is not None
        assert "bot" not in _lane_names(pack)
        assert not any("private vendor list" in t for t in _texts(pack))

        # A Project bot's shared ledger is the Project's, and stays.
        project_task.direct_bot = "scribe"
        await s.flush()
        pack = await context_ops.assemble(s, project_task)
        assert pack is not None
        bot_lanes = [lane for lane in pack.lanes if lane.lane == "bot"]
        assert len(bot_lanes) == 1
        assert any("auto-renewal" in item.text for item in bot_lanes[0].items)
        assert "user" not in _lane_names(pack)


def _fake_light_agent(monkeypatch: pytest.MonkeyPatch, result: object = None) -> list[str]:
    """Stand in for the model. Records every call, so 'never reached the model' is an assertion
    rather than a swallowed exception (both extractors catch their own errors)."""
    calls: list[str] = []

    async def fake(session: object, task: object, name: str, brief: str) -> object:
        calls.append(name)
        if result is None:
            raise AssertionError(f"the {name} must not reach the model on a Project task")
        return result

    monkeypatch.setattr("prismoid.operations.planning.run_light_agent", fake)
    return calls


async def _steered_and_delivered(s: AsyncSession, bob: Actor, task: Task) -> None:
    """Bob steers Ada's Project task, and it lands delivered."""
    task.transition(TaskStatus.PLANNING)
    task.transition(TaskStatus.RUNNING)
    await s.commit()
    await steering.steer(s, bob, task.id, "focus on pricing")
    seq = await task.next_seq(s)
    await TaskEvent.append(
        s, TaskDelivered(seq=seq, task_id=TaskId(task.id), at=utcnow(), summary="Compared the trackers.")
    )
    await s.commit()


async def _written(s: AsyncSession, task_id: uuid.UUID) -> list[MemoryWritten]:
    return [e for e in await TaskEvent.replay(s, task_id, since=0) if isinstance(e, MemoryWritten)]


async def test_memory_extract_stands_down_on_a_project_task(
    silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake_light_agent(monkeypatch)
    async with session_scope() as s:
        ada, bob, _project, task, _personal = await _room(s)
        await _steered_and_delivered(s, bob, task)
        before = len(await Memory.for_scope(s, MemoryScope.USER, user_id=ada.id))

        assert await memory_ops.remember(s, task) == 0
        await s.commit()

        events = await _written(s, task.id)
        assert len(events) == 1
        assert events[0].count == 0 and events[0].skipped == "project"
        assert events[0].items == [] and events[0].turn == task.turn
        assert len(await Memory.for_scope(s, MemoryScope.USER, user_id=ada.id)) == before
        assert calls == [], "memory_extract never reached the model"

        assert await memory_ops.remember(s, task) == 0, "a replay in the same turn does nothing"
        assert len(await _written(s, task.id)) == 1, "the replay guard appends nothing"

        task.turn = 2  # the next turn's delivery is its own, and journals its own skip
        assert await memory_ops.remember(s, task) == 0
        await s.commit()
        events = await _written(s, task.id)
        assert [(e.turn, e.skipped) for e in events] == [(1, "project"), (2, "project")]
        assert calls == []


async def test_a_task_taken_home_with_a_teammates_words_mints_no_private_memory(
    silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fence's other direction: Bob steered Ada's Project task, and she takes it home. Its next
    delivery would brief memory_extract with Bob's words - a steer can become a standing
    "always ..." instruction in HER private memory, steering every personal task she runs. Work
    that carries a teammate's words writes nothing private, wherever it lives now."""
    calls = _fake_light_agent(monkeypatch)
    async with session_scope() as s:
        ada, bob, _project, task, _personal = await _room(s)
        await _steered_and_delivered(s, bob, task)
        await steering.move(s, ada, task.id, None)
        assert task.project_id is None

        assert await memory_ops.remember(s, task) == 0
        await s.commit()
        events = await _written(s, task.id)
        assert [(e.count, e.skipped) for e in events] == [(0, "shared")]
        assert calls == [], "memory_extract never reached the model"


async def test_a_fork_carrying_a_teammates_words_mints_no_private_memory(
    silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fork carries its parent's conversation up to the fork point, so Bob's steer in Ada's
    Project task is in her fork's thread even though the fork's own record holds none of it (owner's
    option B through forks). Taken home and delivered, the fork mints nothing private."""
    calls = _fake_light_agent(monkeypatch)
    async with session_scope() as s:
        ada, bob, _project, task, _personal = await _room(s)
        await _steered_and_delivered(s, bob, task)
        fork = await task_ops.fork(s, ada, task.id)
        await steering.move(s, ada, fork.id, None)
        seq = await fork.next_seq(s)
        await TaskEvent.append(
            s, TaskDelivered(seq=seq, task_id=TaskId(fork.id), at=utcnow(), summary="Done.")
        )
        await s.commit()

        assert await memory_ops.remember(s, fork) == 0
        await s.commit()
        events = await _written(s, fork.id)
        assert [(e.count, e.skipped) for e in events] == [(0, "shared")]
        assert calls == [], "memory_extract never reached the model"


async def test_memory_extract_still_runs_on_a_personal_task(
    silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fence is on Project work only: a personal task's extraction is untouched, and its
    record carries no `skipped` marker."""
    calls = _fake_light_agent(monkeypatch, MemoryExtract(items=[]))
    async with session_scope() as s:
        _ada, _bob, _project, _project_task, personal = await _room(s)
        seq = await personal.next_seq(s)
        await TaskEvent.append(
            s, TaskDelivered(seq=seq, task_id=TaskId(personal.id), at=utcnow(), summary="Done.")
        )

        assert await memory_ops.remember(s, personal) == 0
        events = await _written(s, personal.id)
        assert calls == ["memory_extract"]
        assert len(events) == 1 and events[0].skipped is None


async def test_bot_memory_extract_stands_down_on_a_project_task(
    silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake_light_agent(monkeypatch)
    async with session_scope() as s:
        ada, bob, _project, task, _personal = await _room(s)
        await bot_ops.push(s, actor=ada, name="reviewer", source=_source("reviewer"))
        await memory_ops.create(
            s,
            ada,
            scope=MemoryScope.BOT,
            kind=MemoryKind.FACT,
            text="Ada's private vendor list lives in her drive.",
            bot_name="reviewer",
        )
        task.direct_bot = "reviewer"
        await _steered_and_delivered(s, bob, task)
        before = len(await Memory.for_scope(s, MemoryScope.BOT, user_id=ada.id, bot_name="reviewer"))

        assert await memory_ops.remember_for_bot(s, task) == 0
        await s.commit()

        assert calls == [], "memory_extract never reached the model"
        journal = await TaskEvent.replay(s, task.id, since=0)
        assert not [e for e in journal if isinstance(e, BotMemoryWritten)]
        assert len(await Memory.for_scope(s, MemoryScope.BOT, user_id=ada.id, bot_name="reviewer")) == before


async def test_bot_memory_extract_still_runs_on_a_personal_direct_task(
    silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control for the stand-down above: the same personal bot on a Project-less task is
    remembered as ever - so the fake really is on the path the Project case avoids."""
    calls = _fake_light_agent(monkeypatch, MemoryExtract(items=[]))
    async with session_scope() as s:
        ada, _bob, _project, _project_task, personal = await _room(s)
        await bot_ops.push(s, actor=ada, name="reviewer", source=_source("reviewer"))
        personal.direct_bot = "reviewer"
        await s.flush()

        assert await memory_ops.remember_for_bot(s, personal) == 0
        await s.commit()

        assert calls == ["memory_extract"]
        journal = await TaskEvent.replay(s, personal.id, since=0)
        assert len([e for e in journal if isinstance(e, BotMemoryWritten)]) == 1


# ── history from before a move into a Project ─────────────────────────────────────────────────


@pytest.fixture
async def client(silent_dispatcher: None) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as c:
        yield c
    await auth_ops.flush_outbound_email()
    await hub.stop()  # the route registered on the hub; stop its LISTEN before the engine goes


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _signup(client: AsyncClient, name: str) -> tuple[str, str, dict[str, Any]]:
    email = f"fence{uuid.uuid4().hex[:12]}@example.com"
    r = await client.post(
        "/v1/auth/signup", json={"email": email, "name": name, "password": "a-long-enough-password"}
    )
    assert r.status_code == 201, r.text
    token = str(r.json()["token"])
    me = (await client.get("/v1/auth/me", headers=_h(token))).json()
    return token, email, me


async def test_a_moved_tasks_private_history_is_the_creators_to_read(client: AsyncClient) -> None:
    """A personal task's earlier turn loaded Ada's user lane, minted a memory and handed a step
    her codeword; then she moved it into a Project where Bob views. The rows stay as written -
    only what Bob is served changes."""
    ada_token, _ada_email, ada_me = await _signup(client, "Ada")
    bob_token, bob_email, bob_me = await _signup(client, "Bob")
    ada_id, workspace_id = uuid.UUID(ada_me["id"]), uuid.UUID(ada_me["workspace"]["id"])

    async with session_scope(workspace_id) as s:
        ada = await s.get(Actor, ada_id)
        assert ada is not None
        await memory_ops.create(s, ada, scope=MemoryScope.USER, kind=MemoryKind.FACT, text=CODEWORD)
        await WorkspaceMember.add(s, workspace_id, uuid.UUID(bob_me["id"]), WorkspaceRole.MEMBER)
        await s.commit()
    switched = await client.put(
        "/v1/auth/session/workspace", headers=_h(bob_token), json={"workspace_id": str(workspace_id)}
    )
    assert switched.status_code == 200, switched.text

    created = await client.post(
        "/v1/tasks",
        headers=_h(ada_token),
        json={"goal": "compare three trackers", "clarify": "never", "budget": {"usd": "1"}},
    )
    assert created.status_code == 202, created.text
    task_id = uuid.UUID(created.json()["task_id"])

    # The pre-fence turn, written as it was: the personal task's pack carries her user lane.
    async with session_scope(workspace_id) as s:
        task = await s.get(Task, task_id)
        assert task is not None and task.project_id is None
        pack = await context_ops.assemble(s, task)
        assert pack is not None and any("PINEAPPLE" in i.text for lane in pack.lanes for i in lane.items)
        await context_ops.record(s, task, pack)
        memory = (await Memory.for_scope(s, MemoryScope.USER, user_id=ada_id))[0]
        stub = MemoryStub(id=MemoryId(memory.id), kind="fact", text=CODEWORD)
        seq = await task.next_seq(s)
        await TaskEvent.append(
            s, MemoryWritten(seq=seq, task_id=TaskId(task_id), at=utcnow(), count=1, items=[stub])
        )
        seq = await task.next_seq(s)
        await TaskEvent.append(
            s,
            StepPrompt(
                seq=seq,
                task_id=TaskId(task_id),
                at=utcnow(),
                attempt=1,
                agent="writer",
                prompt=f"...{CODEWORD}...",
            ),
        )
        await s.commit()

    project = await client.post(
        "/v1/projects", headers=_h(ada_token), json={"name": f"fence-{uuid.uuid4().hex[:6]}"}
    )
    assert project.status_code == 201, project.text
    project_id = project.json()["id"]
    shared = await client.post(
        f"/v1/projects/{project_id}/members",
        headers=_h(ada_token),
        json={"email": bob_email, "role": "viewer"},
    )
    assert shared.status_code == 201, shared.text
    moved = await client.post(
        f"/v1/tasks/{task_id}/move", headers=_h(ada_token), json={"project_id": project_id}
    )
    assert moved.status_code == 200, moved.text

    bobs = await client.get(f"/v1/tasks/{task_id}/events?since=0&follow=false", headers=_h(bob_token))
    adas = await client.get(f"/v1/tasks/{task_id}/events?since=0&follow=false", headers=_h(ada_token))

    assert bobs.status_code == 200 and adas.status_code == 200
    for kind in ("context.assembled", "memory.written", "step.prompt", "task.moved"):
        assert kind in bobs.text, f"Bob is served the {kind} frame, redacted - not dropped"
    assert "PINEAPPLE" not in bobs.text
    assert adas.text.count("PINEAPPLE") >= 3, "the creator reads her own record whole"

    # The WebSocket framing is the same door (a regression there would be a silent leak).
    bob_ws = "\n".join(await _ws_replay(task_id, bob_token))
    ada_ws = "\n".join(await _ws_replay(task_id, ada_token))
    for kind in ("context.assembled", "memory.written", "step.prompt", "task.moved"):
        assert kind in bob_ws, f"Bob's socket is served the {kind} frame, redacted - not dropped"
    assert "PINEAPPLE" not in bob_ws
    assert ada_ws.count("PINEAPPLE") >= 3

    # And the operation-level replay (any future list route's source) redacts too.
    async with session_scope(workspace_id) as s:
        bob = await s.get(Actor, uuid.UUID(bob_me["id"]))
        ada = await s.get(Actor, ada_id)
        assert bob is not None and ada is not None
        bob_replay = "".join(e.model_dump_json() for e in await task_ops.replay_events(s, bob, task_id))
        ada_replay = "".join(e.model_dump_json() for e in await task_ops.replay_events(s, ada, task_id))
    assert "context.assembled" in bob_replay and "PINEAPPLE" not in bob_replay
    assert ada_replay.count("PINEAPPLE") >= 3


async def _ws_replay(task_id: uuid.UUID, token: str) -> list[str]:
    """`/v1/tasks/{id}/stream?follow=false`, driven at the ASGI level as a WebSocket client
    would: the text frames the server sent before it closed."""
    app = create_app()
    sent: list[str] = []
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    await inbox.put({"type": "websocket.connect"})

    async def send(message: MutableMapping[str, Any]) -> None:
        if message["type"] == "websocket.send":
            sent.append(str(message.get("text")))

    path = f"/v1/tasks/{task_id}/stream"
    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "scheme": "ws",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"since=0&follow=false",
        "headers": [(b"host", b"test"), (b"authorization", f"Bearer {token}".encode())],
        "client": ("127.0.0.1", 1),
        "server": ("test", 80),
        "subprotocols": [],
    }
    async with asyncio.timeout(10):
        await app(scope, inbox.get, send)
    assert sent, "the socket replayed nothing"
    return sent


# ── a move INTO a Project fences the frozen pack too (the fix-round gap) ────────────────────────
#
# `assemble` fences at planning, but `tasks.context_pack` is FROZEN for the turn and a running
# task can be moved: the pack is then mounted as /task/context.md for every remaining step,
# inlined for tool-less agents, and reused by replan. `steering.move` sheds the private lanes.


class _Frame:
    """The frame's write side, recording what a step would be able to read under /task."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    async def write_bytes(self, path: str, data: bytes, *, mode: int, owner: str) -> None:
        self.files[path] = data


async def _mounted_context(s: AsyncSession, task: Task) -> str | None:
    """/task/context.md as `materialise_inputs` writes it - the very mount every step reads."""
    from prismoid.ai.frames.layout import CONTEXT
    from prismoid.operations.execution.mounts import materialise_inputs

    frame = _Frame()
    await materialise_inputs(s, task, frame)
    raw = frame.files.get(CONTEXT)
    return raw.decode() if raw is not None else None


async def _freeze(s: AsyncSession, task: Task, *, extra_skills_lane: bool = False) -> ContextPack:
    """Plan the task's context the way planning does (assemble + record), commit, return the pack."""
    pack = await context_ops.assemble(s, task)
    assert pack is not None
    if extra_skills_lane:  # a lane that must SURVIVE the fence, so the mount has something to render
        skills = ContextLane(lane="skills", items=[ContextItem(text="Use the memo skill.")], tokens=6)
        pack = ContextPack(lanes=[*pack.lanes, skills], total_tokens=pack.total_tokens + 6)
    await context_ops.record(s, task, pack)
    await s.commit()
    return pack


async def test_a_personal_task_moved_into_a_project_sheds_its_private_lanes(
    silent_dispatcher: None,
) -> None:
    async with session_scope() as s:
        ada, _bob, project, _project_task, personal = await _room(s)
        await bot_ops.push(s, actor=ada, name="reviewer", source=_source("reviewer"))
        await memory_ops.create(
            s,
            ada,
            scope=MemoryScope.BOT,
            kind=MemoryKind.FACT,
            text="Ada's private vendor list lives in her drive.",
            bot_name="reviewer",
        )
        personal.direct_bot = "reviewer"
        personal.transition(TaskStatus.PLANNING)
        personal.transition(TaskStatus.RUNNING)
        await s.flush()
        pack = await _freeze(s, personal, extra_skills_lane=True)
        assert {"user", "bot", "skills"} <= set(_lane_names(pack)), "the personal pack carries both lanes"
        before = await _mounted_context(s, personal)
        assert before is not None and "PINEAPPLE-42" in before, "control: the mount showed it"

        await steering.move(s, ada, personal.id, project.id)
        await s.refresh(personal)

        stored = ContextPack.model_validate(personal.context_pack)
        assert _lane_names(stored) == ["skills"]
        assert "PINEAPPLE" not in str(personal.context_pack)
        assert "private vendor list" not in str(personal.context_pack)
        assert stored.total_tokens == 6, "the totals are recomputed over the surviving lanes"
        assert stored.memory_ids() == [], "no dropped memory id survives on the pack"

        after = await _mounted_context(s, personal)
        assert after is not None and "Use the memo skill." in after
        assert "PINEAPPLE" not in after and "private vendor list" not in after


async def test_a_pack_left_with_nothing_is_dropped_not_padded(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        ada, _bob, project, _project_task, personal = await _room(s)
        await _freeze(s, personal)  # only the user lane
        assert personal.context_pack is not None

        await steering.move(s, ada, personal.id, project.id)
        await s.refresh(personal)

        assert personal.context_pack is None, "silence is not padded into ceremony"
        assert await _mounted_context(s, personal) is None


async def test_a_project_bots_lane_survives_a_round_trip_home(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        ada, _bob, project, project_task, _personal = await _room(s)
        await bot_ops.push(s, actor=ada, name="scribe", source=_source("scribe"), project_id=project.id)
        await memory_ops.create(
            s,
            ada,
            scope=MemoryScope.BOT,
            kind=MemoryKind.PREFERENCE,
            text="Dana wants auto-renewal clauses flagged.",
            bot_name="scribe",
            project_id=project.id,
        )
        await project_ops.update(s, ada, project.id, instructions="Cite sources. UK English.")
        project_task.direct_bot = "scribe"
        await s.flush()
        pack = await _freeze(s, project_task)
        assert {"instructions", "project", "bot"} <= set(_lane_names(pack))
        assert "user" not in _lane_names(pack)

        await steering.move(s, ada, project_task.id, None)  # home: nothing to shed
        await s.refresh(project_task)
        assert project_task.context_pack == pack.model_dump(mode="json")

        await steering.move(s, ada, project_task.id, project.id)  # and back in: the Project's own rows stay
        await s.refresh(project_task)
        assert project_task.context_pack == pack.model_dump(mode="json")
        assert "auto-renewal" in (await _mounted_context(s, project_task) or "")


async def test_a_same_named_project_bot_does_not_launder_a_personal_lane(silent_dispatcher: None) -> None:
    """The destination has a Project bot called `reviewer`, but the frozen lane is the PERSONAL
    `reviewer`'s: the carrier test alone would keep it. The rows' own Project decides."""
    async with session_scope() as s:
        ada, _bob, project, _project_task, personal = await _room(s)
        await bot_ops.push(s, actor=ada, name="reviewer", source=_source("reviewer"))
        await memory_ops.create(
            s,
            ada,
            scope=MemoryScope.BOT,
            kind=MemoryKind.FACT,
            text="Ada's private vendor list lives in her drive.",
            bot_name="reviewer",
        )
        await bot_ops.push(s, actor=ada, name="reviewer", source=_source("reviewer"), project_id=project.id)
        personal.direct_bot = "reviewer"
        await s.flush()
        pack = await _freeze(s, personal, extra_skills_lane=True)
        assert "bot" in _lane_names(pack)

        await steering.move(s, ada, personal.id, project.id)
        await s.refresh(personal)

        assert "private vendor list" not in str(personal.context_pack)
        assert "bot" not in _lane_names(ContextPack.model_validate(personal.context_pack))


async def _alpha_task(s: AsyncSession) -> tuple[Actor, Project, Project, Task]:
    """Ada's running task in Project ALPHA, its pack frozen with ALPHA's instructions and Project
    memory (both marked) plus the skills and thread lanes; BRAVO is another Project of hers."""
    ada, _bob, alpha, task, _personal = await _room(s)
    await project_ops.update(s, ada, alpha.id, instructions="ALPHA-SECRET house rules.")
    await memory_ops.create(
        s,
        ada,
        scope=MemoryScope.PROJECT,
        kind=MemoryKind.NOTE,
        text="ALPHA-SECRET launch is in March.",
        project_id=alpha.id,
    )
    bravo = (await project_ops.create(s, ada, name=f"bravo-{uuid.uuid4().hex[:6]}")).project
    task.transition(TaskStatus.PLANNING)
    task.transition(TaskStatus.RUNNING)
    await s.flush()
    pack = await context_ops.assemble(s, task)
    assert pack is not None and {"instructions", "project"} <= set(_lane_names(pack))
    assert any("ALPHA-SECRET" in t_ for t_ in _texts(pack))
    thread = ContextLane(
        lane="thread", items=[ContextItem(text="The conversation so far - 1 turn(s).")], tokens=9
    )
    skills = ContextLane(lane="skills", items=[ContextItem(text="Use the memo skill.")], tokens=6)
    pack = ContextPack(lanes=[*pack.lanes, thread, skills], total_tokens=pack.total_tokens + 15)
    await context_ops.record(s, task, pack)
    await s.commit()
    return ada, alpha, bravo, task


async def _assert_alpha_shed(s: AsyncSession, task: Task) -> None:
    await s.refresh(task)
    stored = ContextPack.model_validate(task.context_pack)
    assert _lane_names(stored) == ["thread", "skills"], "the Project lanes go; thread and skills stay"
    assert "ALPHA-SECRET" not in str(task.context_pack)
    assert stored.total_tokens == 15 and stored.memory_ids() == []
    mounted = await _mounted_context(s, task)
    assert mounted is not None and "Use the memo skill." in mounted
    assert "ALPHA-SECRET" not in mounted


async def test_a_task_moved_between_projects_sheds_the_source_projects_lanes(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        ada, _alpha, bravo, task = await _alpha_task(s)
        assert "ALPHA-SECRET" in (await _mounted_context(s, task) or ""), "control: the mount showed it"

        await steering.move(s, ada, task.id, bravo.id)

        await _assert_alpha_shed(s, task)


async def test_a_detour_through_personal_does_not_carry_the_source_projects_lanes(
    silent_dispatcher: None,
) -> None:
    """ALPHA -> home -> BRAVO: the pack still says ALPHA's things when it enters BRAVO, and
    'no source Project' is not true of it. The lanes' own provenance decides, not the last hop."""
    async with session_scope() as s:
        ada, _alpha, bravo, task = await _alpha_task(s)

        await steering.move(s, ada, task.id, None)
        await steering.move(s, ada, task.id, bravo.id)

        await _assert_alpha_shed(s, task)


async def test_a_pack_assembled_before_a_move_is_fenced_when_it_is_recorded(
    silent_dispatcher: None,
) -> None:
    """The planning race: planning assembles while the task is still personal, holds the pack
    across the planner's provider call, and a move commits in that window. `record` is the one
    place the pack is written, so it re-reads the task's Project under the row lock and fences
    what it is about to store - the stale in-memory task must not decide."""
    async with session_scope() as s:
        ada, _bob, project, _project_task, personal = await _room(s)
        workspace_id = tenant.bound_workspace(s)
        assert workspace_id is not None
        await s.commit()

        pack = await context_ops.assemble(s, personal)  # planning's read: the task is personal
        assert pack is not None and "user" in _lane_names(pack)
        skills = ContextLane(lane="skills", items=[ContextItem(text="Use the memo skill.")], tokens=6)
        pack = ContextPack(lanes=[*pack.lanes, skills], total_tokens=pack.total_tokens + 6)
        assert personal.project_id is None

        async with session_scope(workspace_id) as other:  # the move, committing inside that window
            mover = await other.get(Actor, ada.id)
            assert mover is not None
            await steering.move(other, mover, personal.id, project.id)
        assert personal.project_id is None, "planning's task object is stale, as in the real window"

        await context_ops.record(s, personal, pack)  # planning's commit_plan, after the provider call
        await s.commit()
        await s.refresh(personal)

        assert personal.project_id == project.id
        stored = ContextPack.model_validate(personal.context_pack)
        assert _lane_names(stored) == ["skills"]
        assert "PINEAPPLE" not in str(personal.context_pack)
        assert stored.total_tokens == 6

        events = [e for e in await TaskEvent.replay(s, personal.id, since=0) if e.type == "context.assembled"]
        assert len(events) == 1
        assert "PINEAPPLE" not in events[0].model_dump_json(), (
            "the record's own event carries no private preview"
        )
        assert [lane.lane for lane in events[0].lanes] == ["skills"]
        assert events[0].project_id == project.id

        (codeword,) = await Memory.for_scope(s, MemoryScope.USER, user_id=ada.id)
        assert codeword.use_count == 0, "a fenced-out memory is neither used nor joined to the task"


async def test_a_move_waits_for_a_planning_pass_and_fences_what_it_stored(silent_dispatcher: None) -> None:
    """The other side of the planning race: planning's `record` holds the task's row lock (its
    transaction is not yet committed) when the move arrives. The move must take that lock BEFORE
    it reads the pack, or it fences the stale (empty) pack, changes nothing, and lets the user
    lane planning is about to commit persist on a task that is now in a Project."""
    async with session_scope() as p:
        ada, _bob, project, _project_task, personal = await _room(p)
        workspace_id = tenant.bound_workspace(p)
        assert workspace_id is not None
        await p.commit()

        pack = await context_ops.assemble(p, personal)  # planning, while the task is personal
        assert pack is not None and "user" in _lane_names(pack)
        skills = ContextLane(lane="skills", items=[ContextItem(text="Use the memo skill.")], tokens=6)
        pack = ContextPack(lanes=[*pack.lanes, skills], total_tokens=pack.total_tokens + 6)
        await context_ops.record(p, personal, pack)  # takes the row lock; NOT committed

        async def move() -> None:
            async with session_scope(workspace_id) as m:
                mover = await m.get(Actor, ada.id)
                assert mover is not None
                await steering.move(m, mover, personal.id, project.id)

        mover_task = asyncio.create_task(move())
        try:
            await asyncio.sleep(0.5)
            assert not mover_task.done(), "the move is parked on planning's row lock"
            await p.commit()
            await asyncio.wait_for(mover_task, timeout=15)
        finally:
            if not mover_task.done():
                mover_task.cancel()

        await p.refresh(personal)
        assert personal.project_id == project.id
        stored = ContextPack.model_validate(personal.context_pack)
        assert _lane_names(stored) == ["skills"]
        assert "PINEAPPLE" not in str(personal.context_pack)
        assert stored.total_tokens == 6


# ── the read-time fence: a pack stored before the fence, or by an older worker,
# is fenced where it is READ - at every consumer - not only where it is written.


async def _legacy_project_pack(s: AsyncSession) -> tuple[Actor, Project, Task]:
    """Ada's Project task wearing a pack written straight onto the row, the way a pre-1a planner
    (or an older worker) stored it: her user lane, a personal bot's lane, and a skills lane that
    must survive every read."""
    ada, _bob, project, project_task, _personal = await _room(s)
    (codeword,) = await Memory.for_scope(s, MemoryScope.USER, user_id=ada.id)
    await bot_ops.push(s, actor=ada, name="reviewer", source=_source("reviewer"))
    vendor = await memory_ops.create(
        s,
        ada,
        scope=MemoryScope.BOT,
        kind=MemoryKind.FACT,
        text="Ada's private vendor list lives in her drive.",
        bot_name="reviewer",
    )
    project_task.direct_bot = "reviewer"
    lanes = [
        ContextLane(lane="user", items=[ContextItem(text=CODEWORD, memory_id=codeword.id)], tokens=7),
        ContextLane(lane="bot", items=[ContextItem(text=vendor.text, memory_id=vendor.id)], tokens=9),
        ContextLane(lane="skills", items=[ContextItem(text="Use the memo skill.")], tokens=6),
    ]
    project_task.context_pack = ContextPack(lanes=lanes, total_tokens=22).model_dump(mode="json")
    project_task.transition(TaskStatus.PLANNING)
    project_task.transition(TaskStatus.RUNNING)
    await s.flush()
    return ada, project, project_task


def _private(text: str) -> bool:
    return "PINEAPPLE" in text or "private vendor list" in text


async def test_a_legacy_project_pack_is_fenced_where_it_is_read(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        _ada, _project, task = await _legacy_project_pack(s)

        readable = await context_ops.readable_pack(s, task)

        assert readable is not None and _lane_names(readable) == ["skills"]
        assert readable.total_tokens == 6, "the totals are the surviving lanes'"
        assert readable.memory_ids() == []
        assert _private(str(task.context_pack)), "the stored row is not rewritten - the read is fenced"


async def test_a_legacy_project_pack_never_mounts(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        _ada, _project, task = await _legacy_project_pack(s)
        mounted = await _mounted_context(s, task)
        assert mounted is not None and "Use the memo skill." in mounted
        assert not _private(mounted)


async def test_a_legacy_project_pack_never_inlines_for_a_toolless_agent(silent_dispatcher: None) -> None:

    async with session_scope() as s:
        _ada, _project, task = await _legacy_project_pack(s)
        inline = await ex_run.inline_context(s, task)
        assert "Use the memo skill." in inline
        assert not _private(inline)


class _Seen(Exception):
    """Raised by a stub once it has captured what the Lead's plan would have been handed."""


async def test_a_legacy_project_pack_never_reaches_the_leads_replan(silent_dispatcher: None) -> None:
    """The replan seat reads the turn's memory through the Lead's tail (`express_ambient`), fenced."""
    from prismoid.operations.execution.express import express_ambient

    async with session_scope() as s:
        _ada, _project, task = await _legacy_project_pack(s)
        tail = await express_ambient(s, task)
    assert "Use the memo skill." in tail and not _private(tail)


async def test_a_legacy_project_pack_never_reaches_the_leads_plan(
    silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prismoid.db.models.plan import PlanStep
    from prismoid.operations.turn import commit as turn_commit
    from prismoid.schemas.lead import SubmitPlan

    seen: list[object] = []

    async def capture(*args: object) -> None:
        seen.append(args[-1])
        raise _Seen

    monkeypatch.setattr(turn_commit, "_routed", capture)
    step = {"label": "s1", "agent": "writer", "title": "Write", "goal": "Write it.", "publishes": True}
    ending = SubmitPlan.model_validate({"say": "On it.", "plan": {"deliverable": "files", "steps": [step]}})
    async with session_scope() as s:
        _ada, _project, task = await _legacy_project_pack(s)
        s.add(lead := PlanStep(task_id=task.id, ordinal=0, title="decide"))
        await s.flush()
        with pytest.raises(_Seen):
            await turn_commit.land_plan(s, task, lead, ending)
    (pack,) = seen
    assert isinstance(pack, ContextPack) and _lane_names(pack) == ["skills"]


async def test_a_project_bots_own_lane_is_still_read(silent_dispatcher: None) -> None:
    """The fence is not a blanket: a Project task whose direct bot is that Project's own reads the
    bot's shared ledger, and a personal task reads its own pack whole."""
    async with session_scope() as s:
        ada, _bob, project, project_task, personal = await _room(s)
        await bot_ops.push(s, actor=ada, name="scribe", source=_source("scribe"), project_id=project.id)
        await memory_ops.create(
            s,
            ada,
            scope=MemoryScope.BOT,
            kind=MemoryKind.PREFERENCE,
            text="Dana wants auto-renewal clauses flagged.",
            bot_name="scribe",
            project_id=project.id,
        )
        project_task.direct_bot = "scribe"
        await s.flush()
        stored = await _freeze(s, project_task)
        assert "bot" in _lane_names(stored)
        assert await context_ops.readable_pack(s, project_task) == stored

        mine = await _freeze(s, personal)
        assert "user" in _lane_names(mine)
        assert await context_ops.readable_pack(s, personal) == mine


# ── a Project bot's conversation: a chat-born Project task carries the chat as its
# thread's turn 0, so the conversation itself must read no private lane.


async def test_a_project_bots_conversation_reads_no_user_lane(silent_dispatcher: None) -> None:
    from prismoid.operations import chat, chat_record, chat_reply

    async with session_scope() as s:
        ada, _bob, project, _project_task, _personal = await _room(s)
        await bot_ops.push(s, actor=ada, name="scribe", source=_source("scribe"), project_id=project.id)
        await memory_ops.create(
            s,
            ada,
            scope=MemoryScope.BOT,
            kind=MemoryKind.PREFERENCE,
            text="Dana wants auto-renewal clauses flagged.",
            bot_name="scribe",
            project_id=project.id,
        )
        conv = await chat.open_conversation(s, ada, bot_name="scribe", project_id=project.id)
        bot = await chat_record.bot_row(s, ada.id, "scribe", project.id)

        brief = await chat_reply.memory_context(s, conv, ada, bot, query="what is my codeword", personal=True)

        assert "PINEAPPLE" not in brief, "the owner's private memory stays out of a Project chat"
        assert "auto-renewal" in brief, "the Project bot's own shared ledger still rides"


async def test_a_personal_bots_conversation_still_reads_the_owners_memory(silent_dispatcher: None) -> None:
    from prismoid.operations import chat, chat_record, chat_reply

    async with session_scope() as s:
        ada, _bob, _project, _project_task, _personal = await _room(s)
        await bot_ops.push(s, actor=ada, name="keeper", source=_source("keeper"))
        conv = await chat.open_conversation(s, ada, bot_name="keeper")
        bot = await chat_record.bot_row(s, ada.id, "keeper", None)

        brief = await chat_reply.memory_context(s, conv, ada, bot, query="what is my codeword", personal=True)

        assert "PINEAPPLE-42" in brief


async def test_a_pack_from_another_project_is_fenced_where_it_is_read(silent_dispatcher: None) -> None:
    """A task whose Project changed under a pack nobody fenced (a move before the fence, or by an
    older worker) still holds the SOURCE Project's house rules and notes on its row: the read is
    the same fence as the store, so neither reaches the new Project's members."""
    async with session_scope() as s:
        _ada, _alpha, bravo, task = await _alpha_task(s)
        task.project_id = bravo.id  # the row moved; the frozen pack did not
        await s.flush()

        readable = await context_ops.readable_pack(s, task)

        assert readable is not None and _lane_names(readable) == ["thread", "skills"]
        assert "ALPHA-SECRET" not in str(readable)


# ── the chat a task came from: a chat-born task's thread opens on its conversation
# as turn 0, and a personal conversation is its owner's - written by her bot from her private
# memory. A Project task, or a Project fork of one, never reads it; nor does its report go home over
# the owner's channel to whoever is on the other end.

PRIVATE_DM = "I am leaving in March, keep it between us"


async def _chat_born(
    s: AsyncSession, *, goal: str = "draft my handover plan"
) -> tuple[Actor, Actor, Project, Task]:
    """Ada's personal task, started from her private chat with her personal bot."""
    from prismoid.operations import chat

    ada, bob, project, _project_task, _personal = await _room(s)
    await bot_ops.push(s, actor=ada, name="keeper", source=_source("keeper"))
    conv = await chat.open_conversation(s, ada, bot_name="keeper")
    await chat.append(s, conv, role=chat.MessageRole.USER, text=PRIVATE_DM)
    await chat.append(s, conv, role=chat.MessageRole.BOT, text=f"Noted. {CODEWORD}.", sender="keeper")
    ask = await chat.append(s, conv, role=chat.MessageRole.USER, text=f"/task {goal} please")
    task = await task_ops.create_task(
        s, ada, goal=goal, origin_conversation_id=conv.id, origin_message_seq=ask.seq
    )
    return ada, bob, project, task


async def _thread_md(s: AsyncSession, task: Task) -> str:
    """/task/thread.md as a step would read it (the planner's brief and the Lead's inline
    thread render the same transcript)."""
    from prismoid.ai.context.thread import render_thread_md
    from prismoid.operations import thread as thread_ops

    transcript = await thread_ops.render_thread(s, task, budget_tokens=thread_ops.MOUNT_THREAD_TOKENS)
    return render_thread_md(transcript)


async def test_a_chat_born_task_moved_into_a_project_mounts_no_private_chat(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        ada, bob, project, task = await _chat_born(s)
        assert PRIVATE_DM in await _thread_md(s, task), "control: her own task opens on her chat"

        await steering.move(s, ada, task.id, project.id)
        thread = await _thread_md(s, task)
        assert PRIVATE_DM not in thread and "PINEAPPLE" not in thread
        assert "draft my handover plan" in thread, "the task's own ask still opens its thread"

        # A teammate's fork renders the root's prelude for HIS task, which lives in the Project.
        fork = await task_ops.fork(s, bob, task.id)
        forked = await _thread_md(s, fork)
        assert PRIVATE_DM not in forked and "PINEAPPLE" not in forked
        assert "draft my handover plan" in forked


async def test_a_project_turns_report_never_leaves_over_the_owners_channel(
    silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prismoid.db.models.bot_channel import BotChannel
    from prismoid.db.models.conversation import Conversation
    from prismoid.operations import channels, chat

    carried: list[str] = []

    async def transport(_s: object, _channel: object, _conv: object, text: str) -> bool:
        carried.append(text)
        return True

    monkeypatch.setattr(channels, "transport_reply", transport)
    async with session_scope() as s:
        ada, _bob, project, shared = await _chat_born(s)
        conv = await s.get(Conversation, shared.origin_conversation_id)
        assert conv is not None
        channel = BotChannel(owner_actor_id=ada.id, bot_name="keeper", kind="webhook")
        s.add(channel)
        await s.flush()
        conv.channel_id = channel.id
        mine = await task_ops.create_task(
            s, ada, goal="list my open loops", origin_conversation_id=conv.id, origin_message_seq=1
        )
        await steering.move(s, ada, shared.id, project.id)

        await chat.on_task_settled(s, mine, outcome="delivered", summary="Your three open loops.")
        await chat.on_task_settled(s, shared, outcome="delivered", summary="The team's handover plan.")

        assert carried and all("open loops" in c for c in carried), "her own task still reports home"
        assert not any("handover" in c for c in carried), "the Project's work never leaves over her channel"


async def test_a_project_task_writes_nothing_into_the_owners_private_conversation(
    silent_dispatcher: None,
) -> None:
    """The write half: once Ada's chat-born task lives in the Project, a turn there is
    the Project's - its report, a gate it opens and its card's continuation stay on the task's own
    thread, never in Ada's private chat (Bob's turn landed there as her
    bot's line). Her own personal task still comes home."""
    from prismoid.db.models.conversation import Conversation
    from prismoid.db.models.message import Message
    from prismoid.errors import ValidationFailed
    from prismoid.operations import chat, chat_reply

    async with session_scope() as s:
        ada, bob, project, shared = await _chat_born(s)
        conv = await s.get(Conversation, shared.origin_conversation_id)
        assert conv is not None
        mine = await task_ops.create_task(
            s, ada, goal="list my open loops", origin_conversation_id=conv.id, origin_message_seq=1
        )
        await chat.on_task_settled(s, mine, outcome="delivered", summary="Your three open loops.")
        await chat.on_task_settled(s, shared, outcome="delivered", summary="Ada's own first turn.")
        await steering.move(s, ada, shared.id, project.id)
        shared.turn, shared.turn_actor_id = 2, bob.id

        await chat.on_task_settled(s, shared, outcome="delivered", summary="Bob's take on the plan.")
        await chat.on_gate_opened(s, shared, gate_id=uuid.uuid4(), title="Which owner?", body="")

        texts = [m.text for m in await Message.since(s, conv.id)]
        assert "Ada's own first turn." in texts, "control: before the move it came home"
        assert "Bob's take on the plan." not in texts and "Which owner?" not in texts

        # The moved task's card is the newest in the thread: the bot is handed no card to continue,
        # and its Reply refuses - a turn started from here could never report back.
        assert await chat_reply._card_above(s, conv) is None  # pyright: ignore[reportPrivateUsage]
        with pytest.raises(ValidationFailed):
            await chat._anchor(s, ada, conv, shared.id)  # pyright: ignore[reportPrivateUsage]

        mine.turn = 2
        await chat.on_task_settled(s, mine, outcome="delivered", summary="Two loops closed.")
        texts = [m.text for m in await Message.since(s, conv.id)]
        assert "Two loops closed." in texts, "control: her personal task still comes home"


async def test_a_task_taken_home_writes_nothing_into_the_projects_conversation(
    silent_dispatcher: None,
) -> None:
    """The mirror: a task started in a Project's conversation and moved home becomes its creator's
    private work (it reads her memory from then on), so its turns never post into the Project's
    conversation, where every member reads."""
    from prismoid.db.models.message import Message
    from prismoid.operations import chat

    async with session_scope() as s:
        ada, _bob, project, _project_task, _personal = await _room(s)
        await bot_ops.push(s, actor=ada, name="scribe", source=_source("scribe"), project_id=project.id)
        conv = await chat.open_conversation(s, ada, bot_name="scribe", project_id=project.id)
        ask = await chat.append(s, conv, role=chat.MessageRole.USER, text="/task draft the team's agenda")
        task = await task_ops.create_task(
            s,
            ada,
            goal="draft the team's agenda",
            project_id=project.id,
            origin_conversation_id=conv.id,
            origin_message_seq=ask.seq,
        )
        await steering.move(s, ada, task.id, None)

        await chat.on_task_settled(s, task, outcome="delivered", summary=f"Agenda. {CODEWORD}.")
        assert not any("PINEAPPLE" in m.text for m in await Message.since(s, conv.id))
