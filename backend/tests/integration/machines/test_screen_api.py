# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The screen's control routes (ADR-065): who may take, renew and hand back control, what
each answers, and the lease on the task's Machine - for its owner, an editor, a viewer and a
stranger from another workspace."""

import dataclasses
import datetime as dt
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from prismoid.api.app import create_app
from prismoid.api.hub import hub
from prismoid.conf import settings
from prismoid.db.base import utcnow
from prismoid.db.models.api_key import ApiKey
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.machine import Machine
from prismoid.db.models.project_member import ProjectMember
from prismoid.db.models.task import Task
from prismoid.db.models.workspace import WorkspaceRole
from prismoid.db.session import row_scope, session_scope
from prismoid.operations import projects as project_ops
from prismoid.operations import screen as screen_ops
from prismoid.operations import tasks as task_ops
from prismoid.operations.machines.screen import NoScreen, open_upstream
from prismoid.schemas.machines import MachineState, frame_name
from tests.support.cast import a_workspace, member

pytestmark = pytest.mark.integration

BOB_TAB = {"conn": "tab-bob-0001", "private": False}


@dataclasses.dataclass(frozen=True)
class Keys:
    task_id: uuid.UUID
    ada: str  # the Project owner
    bob: str  # an editor
    cy: str  # a viewer
    zed: str  # another workspace


async def _shared(
    *, state: MachineState = MachineState.IDLE, profile: str = "browser", driver: str = "docker"
) -> Keys:
    async with session_scope() as s:
        ada, workspace = await member(s, "Ada")
        bob, _ = await member(s, "Bob", role=WorkspaceRole.MEMBER, workspace=workspace)
        cy, _ = await member(s, "Cy", role=WorkspaceRole.MEMBER, workspace=workspace)
        zed, zed_workspace = await a_workspace(s, "Zed")
        project = (await project_ops.create(s, ada, name=f"scr-{uuid.uuid4().hex[:6]}")).project
        s.add(ProjectMember(project_id=project.id, actor_id=bob.id, role="editor", added_by=ada.id))
        s.add(ProjectMember(project_id=project.id, actor_id=cy.id, role="viewer", added_by=ada.id))
        task = await task_ops.create_task(s, ada, goal="screen", project_id=project.id)
        row = Machine(
            workspace_id=workspace.id,
            task_id=task.id,
            driver=driver,
            profile=profile,
            state=state,
            state_since=utcnow(),
            last_active_at=utcnow(),
            epoch=1,
        )
        s.add(row)
        await s.flush()
        row.frame_ref = frame_name(row.id, 1)
        raw: list[str] = []
        for who, workspace_id in (
            (ada, workspace.id),
            (bob, workspace.id),
            (cy, workspace.id),
            (zed, zed_workspace.id),
        ):
            key, token = ApiKey.issue(who.id, "t", workspace_id=workspace_id)
            s.add(key)
            raw.append(token)
        await s.commit()
        return Keys(task.id, *raw)


@pytest.fixture(autouse=True)
def computer_use_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.computer_use, "enabled", True)


@pytest.fixture
async def client(recording_dispatcher: Any) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as c:
        yield c


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _url(k: Keys) -> str:
    return f"/v1/tasks/{k.task_id}/screen/control"


def _t(stamp: str) -> dt.datetime:
    return dt.datetime.fromisoformat(stamp)


async def _endings(task_id: uuid.UUID) -> list[str]:
    async with row_scope(Task, task_id) as s:
        events = await s.scalars(
            select(TaskEvent).where(TaskEvent.task_id == task_id).order_by(TaskEvent.seq)
        )
        return [e.payload["reason"] for e in events.all() if e.type == "machine.control_ended"]


async def test_an_editor_takes_control_and_the_task_shows_it(client: AsyncClient) -> None:
    k = await _shared()
    r = await client.post(_url(k), json=BOB_TAB, headers=_h(k.bob))
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"holder", "private", "since", "until", "cap_at"}
    assert body["holder"]["name"] == "Bob" and body["private"] is False and body["cap_at"] is None
    seen = (await client.get(f"/v1/tasks/{k.task_id}", headers=_h(k.cy))).json()["machine"]["control"]
    assert seen["holder"] == body["holder"] and _t(seen["until"]) == _t(body["until"])


async def test_someone_elses_live_lease_is_a_409_that_names_them(client: AsyncClient) -> None:
    k = await _shared()
    await client.post(_url(k), json=BOB_TAB, headers=_h(k.bob))
    r = await client.post(_url(k), json={"conn": "tab-ada-0001"}, headers=_h(k.ada))
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "conflict" and err["message"] == "Bob is driving the browser."


async def test_a_viewer_gets_403_and_a_stranger_404_on_every_control_route(client: AsyncClient) -> None:
    k = await _shared()
    r = await client.post(_url(k), json=BOB_TAB, headers=_h(k.cy))
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
    for method, path, body in (
        ("POST", _url(k), BOB_TAB),
        ("POST", f"{_url(k)}/renew", None),
        ("DELETE", _url(k), None),
    ):
        r = await client.request(method, path, json=body, headers=_h(k.zed))
        assert r.status_code == 404, (method, path)


async def test_a_malformed_tab_id_is_a_422(client: AsyncClient) -> None:
    k = await _shared()
    r = await client.post(_url(k), json={"conn": "no projects allowed"}, headers=_h(k.bob))
    assert r.status_code == 422


@pytest.mark.parametrize(
    ("state", "profile", "driver", "said"),
    [
        (MachineState.ASLEEP, "browser", "docker", "The Machine is asleep."),
        (MachineState.IDLE, "base", "docker", "The Machine has no browser."),
        # A driver with no screen door: a "You are driving" with no screen behind it, and a
        # step parked on a takeover nobody could see until the lease ran out (found on #12+#13).
        (MachineState.IDLE, "browser", "e2b", "This Machine has no live screen yet."),
    ],
)
async def test_only_a_warm_browser_machine_with_a_screen_can_be_driven(
    client: AsyncClient, state: MachineState, profile: str, driver: str, said: str
) -> None:
    k = await _shared(state=state, profile=profile, driver=driver)
    r = await client.post(_url(k), json=BOB_TAB, headers=_h(k.bob))
    assert r.status_code == 409 and r.json()["error"]["message"] == said
    seen = (await client.get(f"/v1/tasks/{k.task_id}", headers=_h(k.cy))).json()["machine"]["control"]
    assert seen is None


async def test_a_machine_with_no_screen_door_opens_no_upstream() -> None:
    """The relay's upstream asks the claim's rule: `no_browser`, before any frame is reached."""
    k = await _shared(driver="e2b")
    async with row_scope(Task, k.task_id) as s:
        row = await Machine.for_task(s, k.task_id)
        assert row is not None
        machine_id = row.id
    with pytest.raises(NoScreen) as no:
        await open_upstream(machine_id)
    assert no.value.reason == "no_browser"


async def test_computer_use_off_is_a_409(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.computer_use, "enabled", False)
    k = await _shared()
    r = await client.post(_url(k), json=BOB_TAB, headers=_h(k.bob))
    assert r.status_code == 409 and r.json()["error"]["message"] == "Computer use is off."


async def test_renew_gives_a_private_lease_a_fresh_window(client: AsyncClient) -> None:
    k = await _shared()
    first = (
        await client.post(_url(k), json={"conn": "tab-bob-0001", "private": True}, headers=_h(k.bob))
    ).json()
    assert first["private"] is True and first["cap_at"] == first["until"]
    r = await client.post(f"{_url(k)}/renew", headers=_h(k.bob))
    assert r.status_code == 200
    renewed = r.json()
    assert renewed["until"] == renewed["cap_at"] and renewed["since"] == first["since"]
    assert _t(renewed["cap_at"]) >= _t(first["cap_at"])


async def test_hand_back_is_204_and_idempotent_and_the_owner_ends_anyones(client: AsyncClient) -> None:
    k = await _shared()
    await client.post(_url(k), json=BOB_TAB, headers=_h(k.bob))
    assert (await client.delete(_url(k), headers=_h(k.cy))).status_code == 403
    assert (await client.delete(_url(k), headers=_h(k.bob))).status_code == 204
    assert (await client.delete(_url(k), headers=_h(k.bob))).status_code == 204  # nothing held
    await client.post(_url(k), json=BOB_TAB, headers=_h(k.bob))
    assert (await client.delete(_url(k), headers=_h(k.ada))).status_code == 204  # the Project owner
    detail = (await client.get(f"/v1/tasks/{k.task_id}", headers=_h(k.ada))).json()
    assert detail["machine"]["control"] is None
    assert await _endings(k.task_id) == ["released", "ended"]


async def test_sleep_now_and_reset_answer_409_while_someone_drives(client: AsyncClient) -> None:
    k = await _shared()
    await client.post(_url(k), json=BOB_TAB, headers=_h(k.bob))
    for verb in ("sleep", "reset"):
        r = await client.post(f"/v1/tasks/{k.task_id}/machine/{verb}", headers=_h(k.ada))
        assert r.status_code == 409 and r.json()["error"]["message"] == "Bob is driving the browser."


class _Socket:
    """A screen socket that keeps nothing: the test reads the relay's own view of it."""

    async def send_bytes(self, data: bytes) -> None:
        return None

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        return None


async def _asleep(machine_id: uuid.UUID) -> Any:
    raise NoScreen("asleep")  # no upstream: the mask is the lease's, never the frame's


async def test_the_control_routes_move_this_processs_relay_before_they_answer(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The private-switch window, closed: a claim re-reads the lease on this process's relay
    before the route answers, so when Bob's "Private" comes back every other screen here is
    already masked - nothing feeds this relay `screen.state` (no socket subscribed the hub). A
    renewal keeps the mask; a hand-back lifts it the same way."""
    here = screen_ops.Relay(open_upstream=_asleep)
    monkeypatch.setattr(screen_ops, "relay", here)
    k = await _shared()
    async with row_scope(Task, k.task_id) as s:
        row = await Machine.for_task(s, k.task_id)
        assert row is not None
        machine_id = row.id
    watching = screen_ops.Viewer(
        socket=_Socket(), actor_id=uuid.uuid4(), conn="tab-cy-0001", task_id=k.task_id, machine_id=machine_id
    )
    await here.attach(watching)
    try:
        assert watching.masked_by is None and hub.subscriber_count(k.task_id) == 0
        claimed = await client.post(
            _url(k), json={"conn": "tab-bob-0001", "private": True}, headers=_h(k.bob)
        )
        assert claimed.status_code == 200, claimed.text
        bob = uuid.UUID(claimed.json()["holder"]["id"])
        assert watching.masked_by == bob
        assert (await client.post(f"{_url(k)}/renew", headers=_h(k.bob))).status_code == 200
        assert watching.masked_by == bob
        assert (await client.delete(_url(k), headers=_h(k.bob))).status_code == 204
        assert watching.masked_by is None
    finally:
        await here.close_machine(machine_id, 1001, "test over")
