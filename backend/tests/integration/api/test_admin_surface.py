# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The admin surface, end to end against the real database (admin_ops' three laws).

The properties pinned here are the ones whose absence is a breach, not a bug: the scope gate
holds at the door; suspension actually suspends (sessions, keys, login, the credential door);
the last admin cannot be revoked; every admin mutation leaves an audit row; the operator's task
door sees the whole population where the user door does not - and the peek itself is audited.

Needs the stack: `just up && just migrate`.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator, Iterable

import pytest
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.routing import BaseRoute

from prismoid.api.app import create_app
from prismoid.db.models.actor import Actor, ActorStatus, Scope
from prismoid.db.models.admin_action import AdminAction
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.session import AuthSession
from prismoid.db.models.task import Task
from prismoid.db.models.user import User
from prismoid.db.session import session_scope
from prismoid.errors import Conflict, Forbidden, NotFound, ValidationFailed
from prismoid.operations import admin_health, admin_ops
from prismoid.operations import auth as auth_ops
from prismoid.operations import tasks as task_ops
from prismoid.schemas.events import TaskStatusChanged
from prismoid.schemas.plan import TaskStatus

pytestmark = pytest.mark.integration


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    async with session_scope() as session:
        yield session


@pytest.fixture(autouse=True)
async def drained_outbox() -> AsyncIterator[None]:
    yield
    await auth_ops.flush_outbound_email()


def an_email() -> str:
    return f"adm{uuid.uuid4().hex[:12]}@example.com"


def _api_routes(routes: Iterable[BaseRoute]) -> list[APIRoute]:
    """Every APIRoute the app serves, nesting included.

    Not `[r for r in app.routes if isinstance(r, APIRoute)]`: FastAPI 0.139 mounts an included
    router as ONE `_IncludedRouter` node - no `.path`, no `.routes`, its members reachable only
    through `.original_router` - so the flat comprehension found four routes and silently proved
    nothing about the other hundred and twenty-five. A test that enumerates a surface must fail
    loudly when it enumerates nothing, which is why the count assertion below is not optional.

    Both nesting attributes are followed, and paths are already absolute on the members, so
    this stays right whichever way a future FastAPI decides to hold its children."""
    found: list[APIRoute] = []
    for route in routes:
        if isinstance(route, APIRoute):
            found.append(route)
        for attr in ("routes", "original_router"):
            child = getattr(route, attr, None)
            nested = getattr(child, "routes", child)
            if nested:
                found.extend(_api_routes(nested))
    return found


async def _email_of(db: AsyncSession, user_id: uuid.UUID) -> str:
    """The profile pair, not the whole operational picture - `get_user_admin` also computes
    spend, task counts and last-seen, and a test that only wants an email should not buy them."""
    pair = await User.with_actor(db, user_id)
    assert pair is not None
    return pair[0].email


async def _person(db: AsyncSession, *, admin: bool = False) -> tuple[Actor, str]:
    """A signed-up human and a live session token; optionally holding the admin scope."""
    result = await auth_ops.signup(
        db, email=an_email(), name="Admin Surface", password="a-long-enough-password"
    )
    actor = await db.get(Actor, result.user.actor_id)
    assert actor is not None
    if admin and not actor.has_scope(Scope.ADMIN):
        actor.scopes = [*actor.scopes, Scope.ADMIN.value]
        await db.commit()
    return actor, result.token


async def test_admin_routes_hold_the_scope_gate_at_the_door(db: AsyncSession) -> None:
    """403 for a plain user, 200 for an admin - and auth/me carries `is_admin` so the web can
    gate the nav entry instead of probing for the 403."""
    _admin, admin_token = await _person(db, admin=True)
    _plain, plain_token = await _person(db)

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        refused = await client.get("/v1/admin/users", headers={"Authorization": f"Bearer {plain_token}"})
        allowed = await client.get("/v1/admin/users", headers={"Authorization": f"Bearer {admin_token}"})
        me_admin = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {admin_token}"})
        me_plain = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {plain_token}"})

    assert refused.status_code == 403
    assert allowed.status_code == 200
    assert {"items", "next_cursor"} <= set(allowed.json())
    assert me_admin.json()["is_admin"] is True
    assert me_plain.json()["is_admin"] is False


async def test_every_admin_route_holds_the_gate(db: AsyncSession) -> None:
    """EVERY `/v1/admin/*` route refuses a non-admin - enumerated from the app, not listed here.

    The gate is per-route (`AdminDep` in the signature), so a route added without it is a hole
    no per-route test would ever notice: the test for the route you forgot is the one you also
    forgot to write. Walking `app.routes` means the coverage grows with `api/urls.py` by itself.
    """
    _plain, plain_token = await _person(db)
    app = create_app()
    admin_routes = [
        (r.path, method)
        for r in _api_routes(app.routes)
        if r.path.startswith("/v1/admin/")
        for method in sorted((r.methods or set()) - {"HEAD", "OPTIONS"})
    ]
    assert len(admin_routes) >= 26, "the walk must be finding the admin surface, not an empty list"

    holes: list[str] = []
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for path, method in admin_routes:
            # Any UUID will do: the scope refusal happens at dependency resolution, before the
            # path param is looked up in a table - a 404 here would mean the gate opened first.
            url = re.sub(r"\{[^}]+\}", str(uuid.uuid4()), path)
            response = await client.request(
                method, url, headers={"Authorization": f"Bearer {plain_token}"}, json={}
            )
            if response.status_code != 403:
                holes.append(f"{method} {path} → {response.status_code}")
    assert not holes, "admin routes reachable without the admin scope: " + "; ".join(holes)


async def test_the_credential_door_refuses_a_suspended_account(db: AsyncSession) -> None:
    """Law 3, proven where it is enforced. `suspend_user` revokes the live credentials, so the
    ordinary path never reaches this check - what does is the race the check exists for: a
    credential minted before the flip, or a stream re-proving itself. Flipping the
    status underneath a LIVE token is exactly that race, made on purpose."""
    target, token = await _person(db)
    target_id = target.id

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        before = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert before.status_code == 200

        actor = await db.get(Actor, target_id)
        assert actor is not None
        actor.status = ActorStatus.SUSPENDED.value
        await db.commit()

        after = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert after.status_code == 403, "a live token on a suspended account must die at the door"

    actor = await db.get(Actor, target_id)  # leave the shared database as we found it
    assert actor is not None
    actor.status = ActorStatus.ACTIVE.value
    await db.commit()


async def test_suspend_actually_suspends(db: AsyncSession) -> None:
    """The kill switch: sessions and keys revoked, the old token dead at the door, a fresh login
    refused, and the whole act on the audit trail with its reason."""
    admin, _ = await _person(db, admin=True)
    target, target_token = await _person(db)
    admin_id, target_id = admin.id, target.id
    target_email = await _email_of(db, target_id)

    await admin_ops.suspend_user(db, admin, target_id, reason="runaway spend")

    refreshed = await db.get(Actor, target_id)
    assert refreshed is not None and refreshed.status == ActorStatus.SUSPENDED.value
    # The session that was live a moment ago no longer resolves (revoked at suspend).
    db.expire_all()
    assert await AuthSession.resolve(db, target_token) is None
    # And a fresh login is refused outright - no minting sessions that 403 on every use.
    with pytest.raises(Forbidden):
        await auth_ops.login(db, email=target_email, password="a-long-enough-password")

    row = await db.scalar(
        select(AdminAction)
        .where(AdminAction.action == "user.suspend", AdminAction.subject_id == str(target_id))
        .order_by(AdminAction.created_at.desc())
    )
    assert row is not None
    assert row.admin_actor_id == admin_id
    assert row.detail["reason"] == "runaway spend"

    # Unsuspend turns the account back on; nothing revoked resurrects.
    fresh_admin = await db.get(Actor, admin_id)
    assert fresh_admin is not None
    await admin_ops.unsuspend_user(db, fresh_admin, target_id)
    db.expire_all()
    back = await db.get(Actor, target_id)
    assert back is not None and back.status == ActorStatus.ACTIVE.value
    assert await AuthSession.resolve(db, target_token) is None


async def test_suspend_cancels_every_task_the_person_left_running(db: AsyncSession) -> None:
    """The kill switch stops the work too: a queued, parked or running task would otherwise keep
    spending and keep using the person's connectors after their credentials are gone."""
    admin, _ = await _person(db, admin=True)
    target, _ = await _person(db)
    admin_id, target_id = admin.id, target.id
    running = await task_ops.create_task(db, target, goal="a task the suspension must stop")
    running_id = running.id
    admin = await db.get(Actor, admin_id)  # re-fetch: create_task's commit expired the instance
    assert admin is not None

    await admin_ops.suspend_user(db, admin, target_id, reason="abuse")

    db.expire_all()
    stopped = await db.get(Task, running_id)
    assert stopped is not None and stopped.status == TaskStatus.CANCELLED.value
    row = await db.scalar(
        select(AdminAction)
        .where(AdminAction.action == "user.suspend", AdminAction.subject_id == str(target_id))
        .order_by(AdminAction.created_at.desc())
    )
    assert row is not None and row.detail["tasks_cancelled"] == 1


async def test_suspend_refuses_self_and_admins(db: AsyncSession) -> None:
    admin, _ = await _person(db, admin=True)
    other_admin, _ = await _person(db, admin=True)
    with pytest.raises(ValidationFailed):
        await admin_ops.suspend_user(db, admin, admin.id, reason="oops")
    with pytest.raises(ValidationFailed):  # revoke the scope first - two explicit acts
        await admin_ops.suspend_user(db, admin, other_admin.id, reason="rogue")


async def test_grant_revoke_and_the_last_admin_guard(db: AsyncSession) -> None:
    admin, _ = await _person(db, admin=True)
    plain, _ = await _person(db)
    admin_id, plain_id = admin.id, plain.id

    await admin_ops.grant_admin(db, admin, plain_id)
    db.expire_all()
    granted = await db.get(Actor, plain_id)
    assert granted is not None and granted.has_scope(Scope.ADMIN)

    admin2 = await db.get(Actor, admin_id)
    assert admin2 is not None
    await admin_ops.revoke_admin(db, admin2, plain_id)
    db.expire_all()
    revoked = await db.get(Actor, plain_id)
    assert revoked is not None and not revoked.has_scope(Scope.ADMIN)

    # The guard counts ACTIVE admins; with the developer's own login possibly holding the scope
    # in a shared dev database, prove the rule at the boundary instead: strip every OTHER admin's
    # scope inside this transaction, then the last revoke must refuse.
    admin3 = await db.get(Actor, admin_id)
    assert admin3 is not None
    others = list(
        await db.scalars(
            select(Actor).where(Actor.scopes.contains([Scope.ADMIN.value]), Actor.id != admin_id)
        )
    )
    for row in others:
        row.scopes = [s for s in row.scopes if s != Scope.ADMIN.value]
    await db.flush()
    with pytest.raises(ValidationFailed):
        await admin_ops.revoke_admin(db, admin3, admin_id)
    await db.rollback()  # leave the shared database's scopes as we found them


async def test_the_task_console_sees_the_whole_population_and_cancel_works(
    db: AsyncSession,
) -> None:
    """The operator door: another user's running task is listable, peekable (audited), and
    cancellable - while `Task.get_for` still refuses the admin as a stranger (law 1: the user
    door did not widen)."""
    admin, _ = await _person(db, admin=True)
    owner, _ = await _person(db)
    admin_id, owner_id = admin.id, owner.id

    task = await task_ops.create_task(db, owner, goal="a task the operator must be able to see")
    row = await db.get(Task, task.id)
    assert row is not None
    row.transition(TaskStatus.PLANNING)
    row.transition(TaskStatus.RUNNING)
    await db.commit()

    task_id = task.id
    admin = await db.get(Actor, admin_id)  # re-fetch: create_task's commit expired the instance
    assert admin is not None

    # The user door stays narrow: the admin cannot see it as a user.
    with pytest.raises(NotFound):
        await Task.get_for(db, task_id, admin)

    # The operator door: listed with owner identity and a live status.
    page = await admin_ops.list_tasks_admin(db, creator_id=owner_id)
    assert any(r["task"].id == task_id for r in page)

    # The peek is audited (law 2).
    peek = await admin_ops.get_task_admin(db, admin, task_id)
    assert peek["task"].id == task_id
    view = await db.scalar(
        select(AdminAction).where(AdminAction.action == "task.view", AdminAction.subject_id == str(task_id))
    )
    assert view is not None and view.admin_actor_id == admin_id

    # The kill switch runs the user door's machinery and records its reason.
    status = await admin_ops.cancel_admin(db, admin, task_id, reason="wedged in a loop")
    assert status == TaskStatus.CANCELLED.value
    db.expire_all()
    cancelled = await db.get(Task, task_id)
    assert cancelled is not None and cancelled.state is TaskStatus.CANCELLED
    kill = await db.scalar(
        select(AdminAction).where(AdminAction.action == "task.cancel", AdminAction.subject_id == str(task_id))
    )
    assert kill is not None and kill.detail["reason"] == "wedged in a loop"
    # An operator's cancel is a human act and names them on the record.
    flips = [
        e
        for e in await TaskEvent.replay(db, task_id, since=0)
        if isinstance(e, TaskStatusChanged) and e.status is TaskStatus.CANCELLED
    ]
    assert [e.actor.id if e.actor else None for e in flips] == [admin_id]


async def test_stuck_reason_mirrors_the_filter(db: AsyncSession) -> None:
    """One definition of stuck, proven from both sides: a fresh running task is not flagged;
    a running task whose record shows no motion past the threshold is flagged by the SQL filter
    AND explained by the reason line; a deliberately-held larval task never is.

    `task_events` is append-only at the database (UPDATE forbidden by trigger), so the stalled
    case is built as a task with NO events - the predicate's `coalesce` then reads the task's
    own (mutable) `updated_at`, which is also exactly the shape of a run wedged before its
    first durable frame."""
    import datetime as dt

    from sqlalchemy import text as sql_text

    from prismoid.db.base import utcnow

    owner, _ = await _person(db)
    owner_id = owner.id

    fresh_task = await task_ops.create_task(db, owner, goal="a task that is fine")
    fresh_id = fresh_task.id

    wedged = Task(
        goal="a run wedged before its first frame",
        creator_actor_id=owner_id,
        status=TaskStatus.CREATED.value,
    )
    wedged.transition(TaskStatus.PLANNING)
    wedged.transition(TaskStatus.RUNNING)
    db.add(wedged)
    held = Task(
        goal="a held fork, deliberately parked",
        creator_actor_id=owner_id,
        status=TaskStatus.CREATED.value,
        held_at=utcnow(),
    )
    db.add(held)
    await db.commit()
    wedged_id, held_id = wedged.id, held.id

    past = utcnow() - dt.timedelta(seconds=admin_ops.STALE_EVENT_S + 60)
    await db.execute(
        sql_text("update tasks set updated_at = :past where id in (:w, :h)"),
        {"past": past, "w": str(wedged_id), "h": str(held_id)},
    )
    await db.commit()
    db.expire_all()  # the raw UPDATE bypassed the ORM; drop the cached instances

    page = {r["task"].id: r for r in await admin_ops.list_tasks_admin(db, creator_id=owner_id)}
    assert page[fresh_id]["stuck_reason"] is None
    assert page[wedged_id]["stuck_reason"] is not None, "the reason line must explain the stall"
    assert page[held_id]["stuck_reason"] is None, "a held task is parked on purpose, never stuck"

    stuck_ids = {r["task"].id for r in await admin_ops.list_tasks_admin(db, creator_id=owner_id, stuck=True)}
    assert wedged_id in stuck_ids, "the SQL filter must flag the stalled run"
    assert fresh_id not in stuck_ids
    assert held_id not in stuck_ids


async def test_health_alerts_and_spend_shapes(db: AsyncSession) -> None:
    """The structured reads return their promised shapes against live data - the dashboard's
    contract, not its numbers (the numbers belong to the shared database)."""
    health = await admin_health.admin_health(db)
    assert {"migration", "worker", "queues", "version_ghosts", "stuck_tasks", "doctor"} <= set(health)
    assert health["worker"]["status"] in {"ok", "stale", "never"}

    alerts = await admin_health.alerts(db)
    assert all({"code", "severity", "count", "detail"} <= set(a) for a in alerts)

    from prismoid.operations import usage as usage_ops

    summary = await usage_ops.admin_summary(db, days=30)
    assert {"total_usd", "calls", "mtd_usd", "by_model", "top_spenders", "zero_cost_calls"} <= set(summary)
    daily = await usage_ops.admin_daily(db, days=7, group="model")
    assert all({"day", "key", "cost_usd", "calls"} <= set(p) for p in daily)


async def test_audit_trail_is_readable_and_ordered(db: AsyncSession) -> None:
    admin, _ = await _person(db, admin=True)
    target, _ = await _person(db)
    admin_id, target_id = admin.id, target.id
    await admin_ops.revoke_sessions(db, admin, target_id)

    rows, users = await admin_ops.list_audit(db, limit=10)
    assert rows, "the mutation above must be on the trail"
    assert rows[0].created_at >= rows[-1].created_at  # newest first
    assert admin_id in users  # the row can be named


async def test_projects_and_bots_observation(db: AsyncSession) -> None:
    """The organizational views: every project and every bot head is visible through the operator
    door with its owner/author named - and the bot DETAIL read lands on the audit trail
    (instructions are an author's content; reading them is an act, like the task peek)."""
    from prismoid.db.models.bot import BotRecord
    from prismoid.db.models.project import Project

    admin, _ = await _person(db, admin=True)
    owner, _ = await _person(db)
    admin_id, owner_id = admin.id, owner.id
    owner_email = await _email_of(db, owner_id)

    project = Project(name="Observation Deck", owner_actor_id=owner_id)
    db.add(project)
    bot_v1 = BotRecord(
        name="observed-bot",
        version=1,
        status="draft",
        owner_actor_id=owner_id,
        manifest={"description": "watches things"},
        instructions="Observe carefully.",
    )
    bot_v2 = BotRecord(
        name="observed-bot",
        version=2,
        status="draft",
        owner_actor_id=owner_id,
        manifest={"description": "watches things better"},
        instructions="Observe more carefully.",
    )
    db.add_all([bot_v1, bot_v2])
    await db.commit()
    project_id, head_id = project.id, bot_v2.id

    projects = await admin_ops.list_projects_admin(db, q="Observation Deck")
    mine = next(r for r in projects if r["project"].id == project_id)
    assert mine["owner"] is not None and mine["owner"].email == owner_email

    detail = await admin_ops.get_project_admin(db, project_id)
    assert detail["project"].id == project_id

    bots = await admin_ops.list_bots_admin(db, q="observed-bot")
    heads = [r for r in bots if r["bot"].name == "observed-bot"]
    assert len(heads) == 1, "one row per bot - the HEAD, not every version"
    assert heads[0]["bot"].version == 2
    assert heads[0]["author"] is not None and heads[0]["author"].email == owner_email

    admin2 = await db.get(Actor, admin_id)
    assert admin2 is not None
    bot_detail = await admin_ops.get_bot_admin(db, admin2, head_id)
    assert bot_detail["bot"].instructions == "Observe more carefully."
    assert [v.version for v in bot_detail["versions"]] == [2, 1]

    view = await db.scalar(
        select(AdminAction).where(AdminAction.action == "bot.view", AdminAction.subject_id == str(head_id))
    )
    assert view is not None and view.admin_actor_id == admin_id


async def test_the_access_tab_lists_credentials_and_revokes_them_one_by_one(db: AsyncSession) -> None:
    """The Access tab's four endpoints: sessions and keys as METADATA (never a hash, never a
    token), and the surgical single revoke - each audited, each scoped to its owner so a
    mistyped id can never end someone else's session."""
    admin, _ = await _person(db, admin=True)
    target, target_token = await _person(db)
    admin_id, target_id = admin.id, target.id

    target_actor = await db.get(Actor, target_id)
    assert target_actor is not None
    _key, _raw = await auth_ops.create_api_key(db, target_actor, "a key to revoke")

    sessions = await admin_ops.list_user_sessions(db, target_id)
    keys = await admin_ops.list_user_keys(db, target_id)
    assert sessions and keys, "the person we just made has one of each"
    assert all(not hasattr(s, "token") for s in sessions)  # metadata only - the token is nowhere
    # Ids to plain locals BEFORE any expire_all: an expired instance reloads itself lazily, and
    # a lazy load in an async session is a MissingGreenlet, not a query.
    live_session_id = next(s.id for s in sessions if s.is_live())
    live_key_id = next(k.id for k in keys if k.revoked_at is None)

    admin = await db.get(Actor, admin_id)
    assert admin is not None

    # Another user's id must not reach this session: the revoke is scoped by owner.
    stranger, _ = await _person(db)
    assert await admin_ops.revoke_user_session(db, admin, stranger.id, live_session_id) is False
    db.expire_all()
    assert await AuthSession.resolve(db, target_token) is not None, "the stranger's call touched nothing"

    admin = await db.get(Actor, admin_id)
    assert admin is not None
    assert await admin_ops.revoke_user_session(db, admin, target_id, live_session_id) is True
    assert await admin_ops.revoke_user_session(db, admin, target_id, live_session_id) is False  # once
    assert await admin_ops.revoke_user_key(db, admin, target_id, live_key_id) is True
    assert await admin_ops.revoke_user_key(db, admin, target_id, live_key_id) is False

    db.expire_all()
    assert await AuthSession.resolve(db, target_token) is None, "the revoked session is dead"

    for action in ("user.revoke_session", "user.revoke_api_key"):
        row = await db.scalar(
            select(AdminAction).where(AdminAction.action == action, AdminAction.subject_id == str(target_id))
        )
        assert row is not None and row.admin_actor_id == admin_id, f"{action} must be on the trail"


async def test_cancelling_a_finished_task_is_refused_not_faked(db: AsyncSession) -> None:
    """The operator door refuses where the user door no-ops (a 409, not a cheerful lie): the
    console used to toast "Cancelled" over a delivered task while the audit row it wrote died
    uncommitted with the early return."""
    admin, _ = await _person(db, admin=True)
    owner, _ = await _person(db)
    admin_id = admin.id

    task = await task_ops.create_task(db, owner, goal="a task that finishes before the operator arrives")
    row = await db.get(Task, task.id)
    assert row is not None
    row.transition(TaskStatus.PLANNING)
    row.transition(TaskStatus.RUNNING)
    row.transition(TaskStatus.DELIVERED)
    await db.commit()
    task_id = task.id

    admin = await db.get(Actor, admin_id)
    assert admin is not None
    with pytest.raises(Conflict):
        await admin_ops.cancel_admin(db, admin, task_id, reason="too late")

    await db.rollback()
    assert (
        await db.scalar(
            select(AdminAction).where(
                AdminAction.action == "task.cancel", AdminAction.subject_id == str(task_id)
            )
        )
        is None
    ), "a refused cancel leaves no record of one"


async def test_health_carries_its_alerts_from_one_reading(db: AsyncSession) -> None:
    """One request, one `read_vitals`: the dashboard polls this every 30 s, and the stuck scan
    and the catalog doctor are the two most expensive queries on the surface. Health and alerts
    computed apart meant paying for both twice a tick."""
    vitals = await admin_health.read_vitals(db)
    health = await admin_health.admin_health(db, vitals)

    assert health["stuck_tasks"] == vitals.stuck_tasks
    assert health["doctor"] == vitals.doctor
    assert isinstance(health["alerts"], list)
    assert all({"code", "severity", "count", "detail"} <= set(a) for a in health["alerts"])
    assert health["alerts"] == await admin_health.alerts(db, vitals), "same reading, same answer"


async def test_the_routing_table_states_the_whole_class_vocabulary(db: AsyncSession) -> None:
    """`chains` says what is CONFIGURED; `classes` says what there is to configure. Without the
    second the console hand-kept the list, and `image-gen` - a live lane with three catalog
    models behind it - was missing from it for the console's whole first life."""
    from prismoid.operations import routing as route_ops
    from prismoid.schemas.model_class import ModelClass

    out = route_ops.to_out(await route_ops.get_routing(db))
    assert out["classes"] == [c.value for c in ModelClass]
    assert "image-gen" in out["classes"]
    assert set(out["chains"]) <= set(out["classes"]), "a chain for a class that does not exist"
