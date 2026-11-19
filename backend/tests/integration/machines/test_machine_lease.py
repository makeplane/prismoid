# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The control lease's operations (ADR-065): who may claim, renew, extend, release and
end it; what each records and says on the stream; the lease-end job; and the clock's edges on real
rows. The database clock the operations read is pinned (`clock`), so every edge
is exact: `until == now` is expired, renewal is throttled at exactly 5 s, a private grace never
passes its cap, a re-claim never moves the cap, and Renew moves both."""

import asyncio
import dataclasses
import datetime as dt
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import delete, select

from prismoid.ai.frames import screen as frames_screen
from prismoid.conf import settings
from prismoid.db.base import utcnow
from prismoid.db.models.actor import Actor
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.machine import Machine
from prismoid.db.models.project_member import ProjectMember
from prismoid.db.models.task import Task
from prismoid.db.models.workspace import WorkspaceRole
from prismoid.db.session import row_scope, session_scope
from prismoid.errors import Conflict, Forbidden, NoMachine, NotFound
from prismoid.operations import live, machines
from prismoid.operations import projects as project_ops
from prismoid.operations import tasks as task_ops
from prismoid.operations.machines import screen as lease_ops
from prismoid.schemas.events import ScreenState
from prismoid.schemas.machines import MachineState, frame_name, seed_holder
from prismoid.schemas.screen import ControlEnd, LeaseView
from tests.support.cast import a_workspace, member

pytestmark = pytest.mark.integration

S = dt.timedelta(seconds=1)
TAB = "tab-0000001"


@dataclasses.dataclass(frozen=True)
class Scene:
    workspace_id: uuid.UUID
    task_id: uuid.UUID
    machine_id: uuid.UUID
    ada: Actor  # owns the Project
    bob: Actor  # an editor
    cy: Actor  # a viewer
    zed: Actor  # another workspace's founder
    zed_workspace_id: uuid.UUID


async def _scene(
    *, state: MachineState = MachineState.IDLE, profile: str = "browser", step_hosts: list[str] | None = None
) -> Scene:
    """A Project task with a warm Machine idle for an hour (so an ending's clock reset shows), its
    frame narrowed and its last step's list (`step_hosts`) kept on the row."""
    async with session_scope() as s:
        ada, workspace = await member(s, "Ada")
        bob, _ = await member(s, "Bob", role=WorkspaceRole.MEMBER, workspace=workspace)
        cy, _ = await member(s, "Cy", role=WorkspaceRole.MEMBER, workspace=workspace)
        zed, zed_workspace = await a_workspace(s, "Zed")
        project = (await project_ops.create(s, ada, name=f"lease-{uuid.uuid4().hex[:6]}")).project
        s.add(ProjectMember(project_id=project.id, actor_id=bob.id, role="editor", added_by=ada.id))
        s.add(ProjectMember(project_id=project.id, actor_id=cy.id, role="viewer", added_by=ada.id))
        task = await task_ops.create_task(s, ada, goal="drive", project_id=project.id)
        hour_ago = utcnow() - dt.timedelta(hours=1)
        row = Machine(
            workspace_id=workspace.id,
            task_id=task.id,
            driver="docker",
            profile=profile,
            state=state,
            state_since=hour_ago,
            last_active_at=hour_ago,
            epoch=1,
            egress_version=5,
            egress_hosts=[],
            step_hosts=step_hosts or [],
        )
        s.add(row)
        await s.flush()
        row.frame_ref = frame_name(row.id, 1)
        await s.commit()
        return Scene(workspace.id, task.id, row.id, ada, bob, cy, zed, zed_workspace.id)


class Clock:
    """The database clock the lease operations read (`db_now`), pinned to a second a test steps."""

    def __init__(self) -> None:
        self.at = utcnow().replace(microsecond=0)

    async def __call__(self, session: object) -> dt.datetime:
        return self.at

    def step(self, seconds: float) -> dt.datetime:
        self.at += dt.timedelta(seconds=seconds)
        return self.at


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    pinned = Clock()
    monkeypatch.setattr(lease_ops, "db_now", pinned)
    return pinned


@pytest.fixture
def states(monkeypatch: pytest.MonkeyPatch) -> list[LeaseView | None]:
    """Every `screen.state` the operations publish, in order (the frame itself: its own test)."""
    seen: list[LeaseView | None] = []

    async def publish(task_id: uuid.UUID, view: LeaseView | None) -> None:
        seen.append(view)

    monkeypatch.setattr(lease_ops, "publish_state", publish)
    return seen


@pytest.fixture(autouse=True)
def computer_use_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.computer_use, "enabled", True)


class _Jobs:
    """Every enqueue with its kwargs (`RecordingDispatcher` keeps only the job and the key)."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict[str, Any]]] = []

    async def enqueue(self, job: str, key: str, **kwargs: Any) -> None:
        self.sent.append((job, key, kwargs))

    async def send(self, key: str, payload: object, topic: str, *, missing_ok: bool = False) -> None:
        return None


@pytest.fixture
def jobs() -> Iterator[_Jobs]:
    from prismoid.operations import dispatch

    found = _Jobs()
    dispatch.set_dispatcher(found)
    yield found
    dispatch.set_dispatcher(dispatch.NullDispatcher())


_MINE: set[uuid.UUID] = set()


@pytest.fixture
def only_our_rows(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The sweep reads only this test's Machines (a pass over the instance would act on others)."""
    sweepable = Machine.sweepable.__func__

    async def ours(cls: type[Machine], session: Any) -> list[Machine]:
        return [r for r in await sweepable(cls, session) if r.id in _MINE]

    _MINE.clear()
    monkeypatch.setattr(Machine, "sweepable", classmethod(ours))
    yield
    _MINE.clear()


def _workspace(sc: Scene, who: Actor) -> uuid.UUID:
    return sc.zed_workspace_id if who.id == sc.zed.id else sc.workspace_id


async def _claim(sc: Scene, who: Actor, *, conn: str = TAB, private: bool = False) -> LeaseView:
    async with session_scope(workspace_id=_workspace(sc, who)) as s:
        return await lease_ops.claim(s, who, sc.task_id, conn=conn, private=private)


async def _extend(sc: Scene, who: Actor) -> LeaseView:
    async with session_scope(workspace_id=_workspace(sc, who)) as s:
        return await lease_ops.extend(s, who, sc.task_id)


async def _release(sc: Scene, who: Actor) -> None:
    async with session_scope(workspace_id=_workspace(sc, who)) as s:
        await lease_ops.release(s, who, sc.task_id)


async def _row(sc: Scene) -> Machine:
    async with session_scope(workspace_id=sc.workspace_id) as s:
        row = await Machine.current(s, sc.machine_id)
        assert row is not None
        s.expunge(row)  # the scope's closing rollback would expire it
        return row


async def _record(sc: Scene, kind: str) -> list[dict[str, Any]]:
    async with row_scope(Task, sc.task_id) as s:
        events = await s.scalars(
            select(TaskEvent).where(TaskEvent.task_id == sc.task_id).order_by(TaskEvent.seq)
        )
        return [e.payload for e in events.all() if e.type == kind]


# ── claim ─────────────────────────────────────────────────────────────────────────────────


async def test_an_editor_takes_control_and_the_record_and_the_stream_say_so(
    clock: Clock, states: list[LeaseView | None], silent_dispatcher: None
) -> None:
    sc = await _scene()
    view = await _claim(sc, sc.bob)
    assert (view.holder_id, view.holder_name, view.private) == (sc.bob.id, "Bob", False)
    assert (view.since, view.until, view.cap_at) == (clock.at, clock.at + 60 * S, None)
    row = await _row(sc)
    assert (row.control_actor_id, row.control_conn) == (sc.bob.id, TAB)
    [claimed] = await _record(sc, "machine.control_claimed")
    assert claimed["actor"]["name"] == "Bob" and claimed["private"] is False
    assert states == [view]


async def test_someone_elses_live_lease_is_a_409_that_names_them(
    clock: Clock, states: list[LeaseView | None], silent_dispatcher: None
) -> None:
    sc = await _scene()
    await _claim(sc, sc.bob)
    with pytest.raises(Conflict, match=r"^Bob is driving the browser\.$"):
        await _claim(sc, sc.ada, conn="tab-ada-00001")
    assert (await _row(sc)).control_actor_id == sc.bob.id


async def test_a_viewer_cannot_claim_and_a_stranger_cannot_see_the_task(
    clock: Clock, states: list[LeaseView | None], silent_dispatcher: None
) -> None:
    sc = await _scene()
    with pytest.raises(Forbidden, match=r"^this action needs the editor role$"):
        await _claim(sc, sc.cy)
    with pytest.raises(NotFound):
        await _claim(sc, sc.zed)
    assert (await _row(sc)).lease() is None and states == []


@pytest.mark.parametrize(
    ("state", "profile", "said"),
    [
        (MachineState.ASLEEP, "browser", "The Machine is asleep."),
        (MachineState.SLEEPING, "browser", "The Machine is asleep."),
        (MachineState.WAKING, "browser", "The Machine is asleep."),
        (MachineState.IDLE, "base", "The Machine has no browser."),
    ],
)
async def test_only_a_warm_browser_machine_can_be_driven(
    clock: Clock,
    states: list[LeaseView | None],
    silent_dispatcher: None,
    state: MachineState,
    profile: str,
    said: str,
) -> None:
    sc = await _scene(state=state, profile=profile)
    with pytest.raises(Conflict) as refused:
        await _claim(sc, sc.bob)
    assert refused.value.message == said


async def test_computer_use_off_refuses_a_claim(
    clock: Clock, states: list[LeaseView | None], silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.computer_use, "enabled", False)
    sc = await _scene()
    with pytest.raises(Conflict, match=r"^Computer use is off\.$"):
        await _claim(sc, sc.bob)


async def test_a_claim_waits_while_a_forks_seed_holds_the_machine(
    clock: Clock, states: list[LeaseView | None], silent_dispatcher: None
) -> None:
    """A fork's seed holds an idle parent (`running`, `seed:<child>`) while it freezes the frame and
    archives `/work`: nobody may take control of a browser frozen under them - try again in a
    moment, when the seed hands the Machine back."""
    sc = await _scene()
    async with session_scope(workspace_id=sc.workspace_id) as s:
        row = await s.get(Machine, sc.machine_id)
        assert row is not None
        row.state, row.held_by = MachineState.RUNNING, seed_holder(uuid.uuid4())
        await s.commit()
    with pytest.raises(Conflict, match=r"^The Machine is busy; try again in a moment\.$"):
        await _claim(sc, sc.bob)
    assert (await _row(sc)).lease() is None and states == []
    async with session_scope(workspace_id=sc.workspace_id) as s:  # the seed hands it back
        row = await s.get(Machine, sc.machine_id)
        assert row is not None
        row.state, row.held_by = MachineState.IDLE, None
        await s.commit()
    assert (await _claim(sc, sc.bob)).holder_name == "Bob"


async def test_a_task_with_no_machine_has_nothing_to_drive_or_hand_back(
    clock: Clock, states: list[LeaseView | None], silent_dispatcher: None
) -> None:
    sc = await _scene()
    async with session_scope(workspace_id=sc.workspace_id) as s:
        await s.execute(delete(Machine).where(Machine.id == sc.machine_id))
        await s.commit()
    with pytest.raises(NoMachine):
        await _claim(sc, sc.bob)
    await _release(sc, sc.bob)  # nothing held: a no-op


# ── the clock's edges on the row ─────────────────────────────────────────


async def test_a_reclaim_rebinds_the_tab_restores_until_and_never_moves_the_cap(
    clock: Clock, states: list[LeaseView | None], silent_dispatcher: None
) -> None:
    """Bob reloads inside the grace - his re-claim keeps the lease (its
    `since`), binds the new tab, and puts `until` back to the cap, which never moves."""
    sc = await _scene()
    first = await _claim(sc, sc.bob, private=True)
    assert first.until == first.cap_at == first.since + 600 * S
    clock.step(100)
    await lease_ops.socket_closed(sc.machine_id, sc.bob.id, TAB)
    assert (await _row(sc)).control_until == clock.at + 30 * S
    last = states[-1]
    assert last is not None and last.until == clock.at + 30 * S  # the grace starts, on the stream
    clock.step(20)
    again = await _claim(sc, sc.bob, conn="tab-reload-01", private=True)
    assert (again.since, again.until, again.cap_at) == (first.since, first.cap_at, first.cap_at)
    assert (await _row(sc)).control_conn == "tab-reload-01"
    assert len(await _record(sc, "machine.control_claimed")) == 1  # a rebind is not a new claim
    assert states[-1] == again  # but it is said: the relay re-reads the row and the new tab drives


async def test_switching_private_on_is_a_new_private_window_and_is_recorded(
    clock: Clock, states: list[LeaseView | None], silent_dispatcher: None
) -> None:
    """Switching Private on is a fresh private claim: `since` restarts now, so the relay's settle
    (`may_drive` counts from `since`) masks every viewer before the holder's input lands. Switching
    it off restarts the silence clock and keeps `since`. Both are recorded as claims."""
    sc = await _scene()
    first = await _claim(sc, sc.bob)
    clock.step(40)
    masked = await _claim(sc, sc.bob, private=True)
    assert masked.since == clock.at != first.since
    assert masked.until == masked.cap_at == clock.at + 600 * S
    clock.step(10)
    plain = await _claim(sc, sc.bob)
    assert (plain.since, plain.until, plain.cap_at) == (masked.since, clock.at + 60 * S, None)
    assert (await _row(sc)).control_since == masked.since
    assert [c["private"] for c in await _record(sc, "machine.control_claimed")] == [False, True, False]


async def test_the_holders_own_connection_coming_back_restores_the_lease(
    clock: Clock, states: list[LeaseView | None], silent_dispatcher: None
) -> None:
    """Bob's socket drops (a 4408) and reconnects on the SAME connection - no
    re-claim. `socket_reopened` gives back what the grace took: the private lease runs to its cap
    again, past the 30 s the grace left it, and the stream says so. Another connection of his, or
    someone else's, restores nothing (the web's Take control here is that re-claim)."""
    sc = await _scene()
    first = await _claim(sc, sc.bob, private=True)
    clock.step(100)
    await lease_ops.socket_closed(sc.machine_id, sc.bob.id, TAB)
    assert (await _row(sc)).control_until == clock.at + 30 * S
    clock.step(1)
    await lease_ops.socket_reopened(sc.machine_id, sc.bob.id, "tab-other-0001")
    await lease_ops.socket_reopened(sc.machine_id, sc.ada.id, TAB)
    assert (await _row(sc)).control_until == clock.at + 29 * S, "only the holder's own connection"
    published = len(states)
    await lease_ops.socket_reopened(sc.machine_id, sc.bob.id, TAB)
    row = await _row(sc)
    assert (row.control_until, row.control_since, row.control_conn) == (first.cap_at, first.since, TAB)
    assert states[-1] is not None and states[-1].until == first.cap_at and len(states) == published + 1
    clock.step(45)  # past the grace it was given
    with pytest.raises(Conflict, match="Bob is driving the browser"):
        await _claim(sc, sc.ada, conn="tab-ada-0001")  # still Bob's: the lease outlived the grace
    await lease_ops.socket_reopened(sc.machine_id, sc.bob.id, TAB)  # nothing left to give back
    assert len(states) == published + 1 and (await _row(sc)).control_until == first.cap_at


async def test_a_non_private_lease_gets_its_minute_back_on_its_own_connection(
    clock: Clock, states: list[LeaseView | None], silent_dispatcher: None
) -> None:
    sc = await _scene()
    await _claim(sc, sc.bob)
    clock.step(10)
    await lease_ops.socket_closed(sc.machine_id, sc.bob.id, TAB)
    clock.step(2)
    await lease_ops.socket_reopened(sc.machine_id, sc.bob.id, TAB)
    assert (await _row(sc)).control_until == clock.at + 60 * S


async def test_a_private_grace_never_passes_its_cap(
    clock: Clock, states: list[LeaseView | None], silent_dispatcher: None
) -> None:
    sc = await _scene()
    first = await _claim(sc, sc.bob, private=True)
    clock.step(590)
    await lease_ops.socket_closed(sc.machine_id, sc.bob.id, "tab-someone-else")  # not the holder's tab
    assert (await _row(sc)).control_until == first.cap_at
    await lease_ops.socket_closed(sc.machine_id, sc.bob.id, TAB)
    assert (await _row(sc)).control_until == first.cap_at  # min(cap, now + 30 s): the cap


async def test_renew_is_throttled_exactly_at_the_five_second_edge(
    clock: Clock, states: list[LeaseView | None], silent_dispatcher: None
) -> None:
    sc = await _scene()
    first = await _claim(sc, sc.bob)
    clock.at = first.since + dt.timedelta(seconds=4, microseconds=999_999)
    await lease_ops.renew(sc.machine_id, sc.bob.id, TAB)
    assert (await _row(sc)).control_until == first.until  # 4.999999 s since the last renewal
    clock.at = first.since + 5 * S
    await lease_ops.renew(sc.machine_id, sc.bob.id, TAB)
    assert (await _row(sc)).control_until == first.since + 65 * S  # exactly 5 s: renewed
    clock.step(30)
    await lease_ops.renew(sc.machine_id, sc.bob.id, "tab-someone-else")  # input from another tab
    await lease_ops.renew(sc.machine_id, sc.ada.id, TAB)  # or another person
    assert (await _row(sc)).control_until == first.since + 65 * S
    assert len(states) == 1  # a renew changes nothing anyone else shows


async def test_a_private_lease_is_never_renewed_by_input(
    clock: Clock, states: list[LeaseView | None], silent_dispatcher: None
) -> None:
    sc = await _scene()
    first = await _claim(sc, sc.bob, private=True)
    clock.step(30)
    await lease_ops.renew(sc.machine_id, sc.bob.id, TAB)
    assert (await _row(sc)).control_until == first.cap_at


async def test_until_equal_to_now_is_expired(
    clock: Clock, states: list[LeaseView | None], silent_dispatcher: None
) -> None:
    """A lease whose `until` IS now is over - Bob's input cannot renew it, Ada's
    claim beats it, and Bob's ending is recorded first, by its clock, with the idle clock reset."""
    sc = await _scene()
    first = await _claim(sc, sc.bob)
    clock.at = first.until
    await lease_ops.renew(sc.machine_id, sc.bob.id, TAB)
    assert (await _row(sc)).control_until == first.until
    won = await _claim(sc, sc.ada, conn="tab-ada-00001")
    assert (won.holder_id, won.since) == (sc.ada.id, clock.at)
    [ended] = await _record(sc, "machine.control_ended")
    assert (ended["reason"], ended["actor"]["name"], ended["held_s"]) == ("expired", "Bob", 60)
    assert [c["actor"]["name"] for c in await _record(sc, "machine.control_claimed")] == ["Bob", "Ada"]
    assert (await _row(sc)).last_active_at == clock.at
    # Bob's late ending says the row's lease as it stands - Ada's - never "nobody".
    assert [v.holder_name if v is not None else None for v in states] == ["Bob", "Ada", "Ada"]


async def test_renew_moves_both_until_and_the_cap(
    clock: Clock, states: list[LeaseView | None], silent_dispatcher: None
) -> None:
    sc = await _scene()
    first = await _claim(sc, sc.bob, private=True)
    clock.step(540)  # nine minutes in: the pane offers Renew
    renewed = await _extend(sc, sc.bob)
    assert renewed.until == renewed.cap_at == clock.at + 600 * S and renewed.since == first.since
    assert states[-1] == renewed
    with pytest.raises(Conflict, match=r"^Bob is driving the browser\.$"):
        await _extend(sc, sc.ada)
    with pytest.raises(Forbidden):
        await _extend(sc, sc.cy)
    await _release(sc, sc.bob)
    with pytest.raises(Conflict, match=r"^Take control first\.$"):
        await _extend(sc, sc.bob)


# ── endings ───────────────────────────────────────────────────────────────────────────────


async def test_the_holder_hands_back_and_the_owner_ends_anyones(
    clock: Clock, states: list[LeaseView | None], silent_dispatcher: None
) -> None:
    sc = await _scene()
    await _claim(sc, sc.bob)
    with pytest.raises(Forbidden):
        await _release(sc, sc.cy)  # not the holder, not the owner
    clock.step(40)  # inside the minute: a run-out lease is the lease-end job's, not Hand back's
    await _release(sc, sc.bob)
    row = await _row(sc)
    assert row.lease() is None and row.last_active_at == clock.at  # every ending restarts the idle clock
    assert states[-1] is None
    await _release(sc, sc.bob)  # a repeat: nothing held, nothing recorded
    await _claim(sc, sc.bob, private=True)
    clock.step(30)
    await _release(sc, sc.ada)  # the Project owner ends Bob's control
    ended = await _record(sc, "machine.control_ended")
    assert [(e["reason"], e["actor"]["name"], e["held_s"]) for e in ended] == [
        ("released", "Bob", 40),
        ("ended", "Bob", 30),
    ]
    assert ended[0]["ended_by"] is None and ended[1]["ended_by"]["name"] == "Ada"


@pytest.mark.parametrize(
    ("private", "socket_closed", "reason", "held_s"),
    [
        (False, False, "expired", 60),  # a minute with no input
        (True, True, "grace", 130),  # the tab closed at 100 s and the 30 s grace ran out
        (True, False, "cap", 600),  # the private lease ran to its cap
    ],
)
async def test_handing_back_a_run_out_lease_ends_it_at_once_by_its_clock(
    clock: Clock,
    states: list[LeaseView | None],
    silent_dispatcher: None,
    private: bool,
    socket_closed: bool,
    reason: str,
    held_s: int,
) -> None:
    """Hand back after the lease's clock ran out, before the lease-end job got to it: it ends now,
    by its own clock - never "driving" on the page for the sweep's 30 s - and once."""
    sc = await _scene()
    first = await _claim(sc, sc.bob, private=private)
    if socket_closed:
        clock.step(100)
        await lease_ops.socket_closed(sc.machine_id, sc.bob.id, TAB)
    clock.at = first.since + 700 * S  # past every clock
    await _release(sc, sc.bob)
    row = await _row(sc)
    assert row.lease() is None and row.last_active_at == clock.at
    [ended] = await _record(sc, "machine.control_ended")
    assert (ended["reason"], ended["actor"]["name"], ended["held_s"], ended["ended_by"]) == (
        reason,
        "Bob",
        held_s,
        None,
    )
    assert states[-1] is None
    await _release(sc, sc.bob)  # nothing held now: a no-op, nothing more recorded
    assert len(await _record(sc, "machine.control_ended")) == 1
    assert not await lease_ops.end_lease(sc.machine_id, since=first.since, reason=None)  # the job's, late


async def test_a_demoted_holder_loses_the_lease(
    clock: Clock, states: list[LeaseView | None], silent_dispatcher: None
) -> None:
    sc = await _scene()
    await _claim(sc, sc.bob)
    assert not await lease_ops.end_live(sc.machine_id, sc.ada.id, reason=ControlEnd.DEMOTED)  # not Ada's
    assert await lease_ops.end_live(sc.machine_id, sc.bob.id, reason=ControlEnd.DEMOTED)
    [ended] = await _record(sc, "machine.control_ended")
    assert (ended["reason"], ended["actor"]["name"]) == ("demoted", "Bob")
    assert (await _row(sc)).lease() is None


async def test_the_sweep_hands_a_run_out_lease_to_its_job_which_ends_it_once(
    clock: Clock, states: list[LeaseView | None], jobs: _Jobs, only_our_rows: None
) -> None:
    sc = await _scene()
    _MINE.add(sc.machine_id)
    clock.at = utcnow() - 120 * S  # claimed two minutes ago: run out a minute ago, by the real clock
    first = await _claim(sc, sc.bob)
    tick = utcnow()
    await machines.sweep_once(tick)
    [(_, key, kw)] = [j for j in jobs.sent if j[0] == "machine_lease_end"]
    assert key == f"machine-lease-{sc.machine_id}-{first.since.isoformat()}~{tick.isoformat()}"
    assert kw == {"machine_id": str(sc.machine_id), "since": first.since.isoformat(), "reason": ""}
    assert not [j for j in jobs.sent if j[0] == "machine_sleep"]  # idle an hour, but leased
    clock.at = first.since + 120 * S
    since = dt.datetime.fromisoformat(kw["since"])
    assert await lease_ops.end_lease(sc.machine_id, since=since, reason=None)
    assert not await lease_ops.end_lease(sc.machine_id, since=since, reason=None)  # once
    row = await _row(sc)
    assert row.lease() is None and row.last_active_at == clock.at
    [ended] = await _record(sc, "machine.control_ended")
    # Held until its clock ran out (`until`), not until the job noticed: a minute, not two.
    assert (ended["reason"], ended["held_s"]) == ("expired", 60)
    assert states[-1] is None


async def test_a_job_leaves_a_lease_renewed_since_the_sweep_and_another_leases_job(
    clock: Clock, states: list[LeaseView | None], silent_dispatcher: None
) -> None:
    sc = await _scene()
    first = await _claim(sc, sc.bob)
    clock.step(30)
    assert not await lease_ops.end_lease(sc.machine_id, since=first.since, reason=None)  # still live
    assert not await lease_ops.end_lease(sc.machine_id, since=first.since - S, reason=ControlEnd.DISABLED)
    assert (await _row(sc)).lease() is not None
    # The kill switch's ending needs no run-out clock.
    assert await lease_ops.end_lease(sc.machine_id, since=first.since, reason=ControlEnd.DISABLED)
    [ended] = await _record(sc, "machine.control_ended")
    assert ended["reason"] == "disabled"


# ── the frame ─────────────────────────────────────────────────────────────────────────────


async def test_screen_state_names_the_holder_and_nothing_more(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[object] = []

    async def notify(frame: object) -> None:
        sent.append(frame)

    monkeypatch.setattr(live, "notify", notify)
    task_id, holder, until = uuid.uuid4(), uuid.uuid4(), utcnow()
    view = LeaseView(
        holder_id=holder, holder_name="Bob", private=True, since=until - 60 * S, until=until, cap_at=until
    )
    await lease_ops.publish_state(task_id, view)
    await lease_ops.publish_state(task_id, None)
    first, second = sent
    assert isinstance(first, ScreenState) and first.holder is not None
    assert (first.holder.id, first.holder.name, first.holder.type) == (holder, "Bob", "user")
    assert (first.private, first.until, first.step_id, first.lane) == (True, until, None, "live")
    assert isinstance(second, ScreenState) and (second.holder, second.private, second.until) == (
        None,
        False,
        None,
    )


# ── what the driver's browser may reach ───────────────────────────────────────────────────


def _egress(jobs: _Jobs) -> list[tuple[str, dict[str, Any]]]:
    """The `machine_egress` jobs enqueued: the API never touches a frame, so a lease that
    moves an idle Machine's egress hands the push to the worker, pinned to the row's version."""
    return [(key, kwargs) for job, key, kwargs in jobs.sent if job == "machine_egress"]


def _push(sc: Scene, version: int) -> tuple[str, dict[str, Any]]:
    return (
        f"machine-egress-{sc.machine_id}-{version}",
        {"machine_id": str(sc.machine_id), "version": version},
    )


async def test_driving_an_idle_machine_opens_its_last_steps_list_and_handing_back_closes_it(
    clock: Clock, states: list[LeaseView | None], jobs: _Jobs
) -> None:
    sc = await _scene(step_hosts=["docs.example", "login.example"])
    await _claim(sc, sc.bob)
    row = await _row(sc)
    assert (row.egress_version, row.egress_hosts) == (6, ["docs.example", "login.example"])
    assert _egress(jobs) == [_push(sc, 6)]
    await _claim(sc, sc.bob, conn="tab-0000002")  # a re-claim (a reload) moves nothing
    await _claim(sc, sc.bob, private=True)  # nor does privacy switched: the same person drives
    assert (await _row(sc)).egress_version == 6 and _egress(jobs) == [_push(sc, 6)]
    await _release(sc, sc.bob)
    row = await _row(sc)
    assert (row.egress_version, row.egress_hosts) == (7, [])
    assert row.step_hosts == ["docs.example", "login.example"], "kept for the next driver"
    assert _egress(jobs) == [_push(sc, 6), _push(sc, 7)]


async def test_every_ending_of_an_idle_machines_lease_narrows_its_frame(
    clock: Clock, states: list[LeaseView | None], jobs: _Jobs
) -> None:
    """The API's endings hand the push to the worker's job; the lease-end job's own ending runs
    inside a DBOS step, which cannot start a job (DBOS asserted, and the frame kept the
    driver's list), so it returns the version for the job's next step to push."""
    for ending in ("ended", "expired", "demoted", "disabled"):
        sc = await _scene(step_hosts=["docs.example"])
        await _claim(sc, sc.bob)
        lease = (await _row(sc)).lease()
        assert lease is not None
        pushes = len(_egress(jobs))
        if ending == "ended":  # the Project owner's End
            await _release(sc, sc.ada)
        elif ending == "expired":  # the lease-end job, after a minute of silence
            clock.step(61)
            ended = await lease_ops.end_lease(sc.machine_id, since=lease.since, reason=None)
            assert ended is not None and ended.egress == 7
        elif ending == "demoted":  # the socket's proof
            assert await lease_ops.end_live(sc.machine_id, sc.bob.id, reason=ControlEnd.DEMOTED)
        else:  # the kill switch, through the same job
            ended = await lease_ops.end_lease(sc.machine_id, since=lease.since, reason=ControlEnd.DISABLED)
            assert ended is not None and ended.egress == 7
        row = await _row(sc)
        assert (row.egress_version, row.egress_hosts) == (7, []), ending
        if ending in ("ended", "demoted"):
            assert _egress(jobs)[pushes:] == [_push(sc, 7)], ending
        else:
            assert _egress(jobs)[pushes:] == [], f"{ending}: the job pushes it, never a job from its step"


async def test_a_running_steps_list_is_the_drivers_and_no_push_is_sent(
    clock: Clock, states: list[LeaseView | None], jobs: _Jobs
) -> None:
    """A step holds the Machine: its list is what the frame has, the driver's included, and the
    step's own hand-back decides what an idle Machine keeps."""
    sc = await _scene(state=MachineState.RUNNING, step_hosts=["old.example"])
    async with session_scope(workspace_id=sc.workspace_id) as s:
        row = await s.get(Machine, sc.machine_id)
        assert row is not None
        row.held_by, row.egress_hosts = "step:00000000-0000-0000-0000-000000000001:1", ["docs.example"]
        await s.commit()
    await _claim(sc, sc.bob)
    await _release(sc, sc.bob)
    row = await _row(sc)
    assert (row.egress_version, row.egress_hosts) == (5, ["docs.example"]) and _egress(jobs) == []


@pytest.fixture
def handed_back(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Every hand-back `after_end` asks a door for, as (driver, ref); the Machine's door has a
    control stream (a remote driver's)."""
    asked: list[tuple[str, str]] = []

    async def hand_back(driver: str, ref: str) -> None:
        asked.append((driver, ref))

    monkeypatch.setattr(frames_screen, "has_control", lambda driver: driver == "remote")
    monkeypatch.setattr(frames_screen, "hand_back", hand_back)
    return asked


async def _on_remote(machine_id: uuid.UUID) -> str:
    async with row_scope(Machine, machine_id) as s:
        row = await Machine.current(s, machine_id)
        assert row is not None
        row.driver = "remote"
        row.frame_ref = "rmt_lease0001"
        await s.commit()
    return "rmt_lease0001"


async def test_every_ending_on_a_warm_remote_machine_hands_the_screen_back(
    clock: Clock,
    states: list[LeaseView | None],
    handed_back: list[tuple[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every way a lease ends calls the door's hand-back - the holder's own, an owner's End."""
    monkeypatch.setattr(lease_ops, "has_screen", lambda profile, driver: True)
    scene = await _scene()
    ref = await _on_remote(scene.machine_id)
    await _claim(scene, scene.bob)
    await _release(scene, scene.bob)
    assert handed_back == [("remote", ref)]


async def test_an_ending_on_an_asleep_machine_never_hands_back(
    clock: Clock,
    states: list[LeaseView | None],
    handed_back: list[tuple[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any call on a sleeping computer wakes it; its sleep handed the screen back."""
    monkeypatch.setattr(lease_ops, "has_screen", lambda profile, driver: True)
    scene = await _scene()
    await _on_remote(scene.machine_id)
    view = await _claim(scene, scene.bob)
    async with row_scope(Machine, scene.machine_id) as s:
        row = await Machine.current(s, scene.machine_id)
        assert row is not None
        held = row.lease()
        row.state = MachineState.ASLEEP
        await s.commit()
    assert held is not None and held.since == view.since
    await lease_ops.after_end(
        lease_ops.Ended(held, ControlEnd.LOST, None, clock.at, scene.task_id, scene.machine_id)
    )
    assert handed_back == []


async def test_an_ending_never_hands_back_over_a_newer_lease(
    clock: Clock,
    states: list[LeaseView | None],
    handed_back: list[tuple[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A claim that landed before `after_end` ran holds the row: a hand-back would end its takeover."""
    monkeypatch.setattr(lease_ops, "has_screen", lambda profile, driver: True)
    scene = await _scene()
    await _on_remote(scene.machine_id)
    older = await _claim(scene, scene.bob)
    async with row_scope(Machine, scene.machine_id) as s:
        row = await Machine.current(s, scene.machine_id)
        assert row is not None
        held = row.lease()
    assert held is not None and held.since == older.since
    await lease_ops.after_end(
        lease_ops.Ended(held, ControlEnd.LOST, None, clock.at, scene.task_id, scene.machine_id)
    )
    assert handed_back == []


@pytest.fixture
def resolved(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every CONTROL gate answer `after_end` asks for, as its keyword arguments."""
    from prismoid.operations import gates

    asked: list[dict[str, Any]] = []

    async def resolve_control(task_id: uuid.UUID, **kw: Any) -> bool:
        asked.append({"task_id": task_id, **kw})
        return True

    monkeypatch.setattr(gates, "resolve_control", resolve_control)
    return asked


async def _ended_on_remote(scene: Scene, clock: Clock) -> lease_ops.Ended:
    """Bob's lease on a warm remote Machine, cleared from the row as its ending's commit does:
    the `Ended` that `after_end` is handed next."""
    await _on_remote(scene.machine_id)
    await _claim(scene, scene.bob)
    async with row_scope(Machine, scene.machine_id) as s:
        row = await Machine.current(s, scene.machine_id)
        assert row is not None
        held = row.clear_lease()
        await s.commit()
    assert held is not None
    return lease_ops.Ended(held, ControlEnd.RELEASED, None, clock.at, scene.task_id, scene.machine_id)


async def test_a_hand_back_that_raises_still_answers_the_control_gate(
    clock: Clock,
    states: list[LeaseView | None],
    resolved: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The door's hand-back failing never leaves the agent parked on its CONTROL
    gate for `control_wait_s` - the gate is answered whatever the hand-back did."""
    monkeypatch.setattr(lease_ops, "has_screen", lambda profile, driver: True)
    scene = await _scene()
    ended = await _ended_on_remote(scene, clock)
    tried: list[str] = []

    async def hand_back(driver: str, ref: str) -> None:
        tried.append(ref)
        raise RuntimeError("the door's hand-back failed")

    monkeypatch.setattr(frames_screen, "has_control", lambda driver: driver == "remote")
    monkeypatch.setattr(frames_screen, "hand_back", hand_back)
    with pytest.raises(RuntimeError):
        await lease_ops.after_end(ended)
    assert tried == ["rmt_lease0001"]
    assert [(r["task_id"], r["lease_since"], r["holder"]) for r in resolved] == [
        (scene.task_id, ended.lease.since, "Bob")
    ]


async def test_a_hand_back_cancelled_while_it_hangs_still_answers_the_control_gate(
    clock: Clock,
    states: list[LeaseView | None],
    resolved: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hand-back can take up to its 5 s bound; `after_end` cancelled in that
    window (a request's end, a worker's shutdown) still answers the gate, after the hand-back."""
    monkeypatch.setattr(lease_ops, "has_screen", lambda profile, driver: True)
    scene = await _scene()
    ended = await _ended_on_remote(scene, clock)
    entered = asyncio.Event()

    async def hand_back(driver: str, ref: str) -> None:
        entered.set()
        await asyncio.Event().wait()  # the provider never answers

    monkeypatch.setattr(frames_screen, "has_control", lambda driver: driver == "remote")
    monkeypatch.setattr(frames_screen, "hand_back", hand_back)
    running = asyncio.create_task(lease_ops.after_end(ended))
    await asyncio.wait_for(entered.wait(), timeout=10)
    assert resolved == []  # the hand-back first, the gate after
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert [(r["task_id"], r["lease_since"]) for r in resolved] == [(scene.task_id, ended.lease.since)]
