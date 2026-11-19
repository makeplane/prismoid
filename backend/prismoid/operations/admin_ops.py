# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The operator's verbs - every admin read and mutation, audited, in one place.

Three laws, and they are the file:

1. **Admin does not widen `Task._visible_to`.** The operator's task reads ride their own explicit
   query path (`Task.admin_page` / `Task.admin_row`) - never a bypass flag threaded through the
   user-facing fetches. Widening `get_for` would silently change every read in the product
   (streams, artifacts, gates) and make operator access unauditable. A separate door can be
   watched; a loosened lock cannot.
2. **Every mutation writes an `AdminAction` row in the same transaction** (`audit()` + the op's
   own commit). The task PEEK is audited too - reading someone else's thread is an act, not a
   glance. List views are not: a page of metadata is the dashboard, not an intrusion.
3. **Suspension is enforced at the credential door** (`api/deps.require_actor`), not here - this
   module only flips the switch and revokes what is live. The 10 s stream re-proof
   severs any open SSE within one interval; nothing extra to build.

The SQL itself lives on the fat models (`User.admin_page`, `Task.admin_page`,
`UsageEvent.window_totals`, …); this file owns the POLICY - guards, audit rows, transactions.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, NamedTuple

import logfire
from pydantic import EmailStr, TypeAdapter
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.base import utcnow
from prismoid.db.models import tenant
from prismoid.db.models.actor import Actor, ActorStatus, Scope
from prismoid.db.models.admin_action import AdminAction
from prismoid.db.models.api_key import ApiKey
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.password_reset import PasswordReset
from prismoid.db.models.session import AuthSession
from prismoid.db.models.task import Task
from prismoid.db.models.user import User
from prismoid.errors import Conflict, NotFound, ValidationFailed
from prismoid.schemas.events import DurableEvent
from prismoid.schemas.plan import TaskStatus

# A task is STUCK when the machine claims motion and the record shows none. The numbers are
# deliberately conservative - the sweeper (jobs/sweeper.py) already re-enqueues true orphans
# within minutes, so anything THESE catch has outlived the automatic belts.
STALE_EVENT_S = 600  # running/waiting with no task_event this long → stalled
UNDISPATCHED_S = 600  # created/planning this long (not held) → never took off


# ---------------------------------------------------------------- the audit pen


def audit(
    session: AsyncSession,
    admin: Actor,
    action: str,
    subject_type: str,
    subject_id: str,
    detail: dict[str, Any] | None = None,
) -> AdminAction:
    """Add the audit row to the CALLER's transaction - the mutation and its record commit or
    roll back together. No commit here: the operation owns the transaction."""
    row = AdminAction(
        admin_actor_id=admin.id,
        action=action,
        subject_type=subject_type,
        subject_id=subject_id,
        detail=detail or {},
    )
    session.add(row)
    return row


async def list_audit(
    session: AsyncSession,
    *,
    subject_type: str | None = None,
    subject_id: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> tuple[list[AdminAction], dict[uuid.UUID, User]]:
    """One audit page, newest first, plus the admin identities to name the rows with.
    The subject pair narrows to one thing's history - the user page's History tab."""
    rows = await AdminAction.page(
        session, subject_type=subject_type, subject_id=subject_id, limit=limit, cursor=cursor
    )
    users = await User.by_actor_ids(session, [r.admin_actor_id for r in rows])
    return rows, users


# ---------------------------------------------------------------- users


async def list_users(
    session: AsyncSession,
    *,
    q: str | None = None,
    status: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> list[tuple[User, Actor]]:
    return await User.admin_page(session, q=q, status=status, limit=limit, cursor=cursor)


async def get_user_admin(session: AsyncSession, user_id: uuid.UUID) -> tuple[User, Actor, dict[str, Any]]:
    """One user, with the operational facts the list is too wide for: live session/key counts,
    task counts by status, and spend (month-to-date + all-time). Metadata only - never hashes."""
    from prismoid.operations import usage as usage_ops

    pair = await User.with_actor(session, user_id)
    if pair is None:
        raise NotFound("no such user")
    user, actor = pair

    month_start = utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    total, mtd = await usage_ops.creator_spend(session, user_id, month_start=month_start)
    # "Last seen" is honest, not omniscient: the newest of (a session's birth, a key's throttled
    # last_used bump). Sessions do not record per-request use - saying so beats faking it.
    seen = [
        t
        for t in (
            await AuthSession.latest_created_at(session, user_id),
            await ApiKey.latest_used_at(session, user_id),
        )
        if t is not None
    ]
    facts = {
        "live_sessions": await AuthSession.live_count_for(session, user_id),
        "live_api_keys": await ApiKey.live_count_for(session, user_id),
        "task_counts": await Task.counts_for_creator(session, user_id),
        "spend_total_usd": total,
        "spend_mtd_usd": mtd,
        "last_seen_at": max(seen) if seen else None,
    }
    return user, actor, facts


def _act_for(session: AsyncSession, workspace_id: uuid.UUID) -> None:
    """The console is PLATFORM, but a write that acts on ONE tenant row acts
    for that row's workspace - bound before the operation, as the sweeps bind per row."""
    tenant.bind(session, workspace_id)


async def _target_actor(session: AsyncSession, user_id: uuid.UUID) -> tuple[User, Actor]:
    pair = await User.with_actor(session, user_id)
    if pair is None:
        raise NotFound("no such user")
    return pair


async def suspend_user(
    session: AsyncSession, admin: Actor, user_id: uuid.UUID, *, reason: str
) -> tuple[User, Actor]:
    """The kill switch. Flips `actors.status`, revokes every live session AND API key (the same
    pair a password reset revokes), and the credential door refuses the
    account from the next request. An open stream dies at its next 10 s re-proof.

    Two refusals, both deliberate: yourself (an admin locked out by their own click is a support
    ticket with no supporter), and another ADMIN (revoke their scope first - suspending an admin
    should be two explicit acts, not one angry one).

    Returns the (profile, principal) pair it already holds - every one of these verbs does, so
    the endpoint serializes the row it just changed instead of re-reading the whole user.
    """
    user, actor = await _target_actor(session, user_id)
    if actor.id == admin.id:
        raise ValidationFailed("you cannot suspend your own account")
    if actor.has_scope(Scope.ADMIN):
        raise ValidationFailed("this user holds the admin scope - revoke it first, then suspend")
    if not actor.is_active():
        return user, actor  # already off; a second flip is a no-op, not an error

    actor.status = ActorStatus.SUSPENDED.value
    revoked_sessions = await AuthSession.revoke_all_for(session, user_id)
    revoked_keys = await ApiKey.revoke_all_for(session, user_id)
    unsettled = await Task.unsettled_of(session, actor.id)
    audit(
        session,
        admin,
        "user.suspend",
        "user",
        str(user_id),
        {
            "reason": reason,
            "sessions_revoked": revoked_sessions,
            "api_keys_revoked": revoked_keys,
            "tasks_cancelled": len(unsettled),
        },
    )
    await session.commit()
    logfire.info("admin {admin} suspended user {user}", admin=str(admin.id), user=str(user_id))
    await _stop_all(session, admin, unsettled)
    return user, actor


async def _stop_all(
    session: AsyncSession, admin: Actor, unsettled: list[tuple[uuid.UUID, uuid.UUID]]
) -> None:
    """Cancel what a suspended person left running, after the suspension committed: a queued,
    parked or running task would otherwise keep spending and keep using their connectors. Each
    cancel binds its task's workspace and commits on its own, so one that fails leaves the
    suspension and the cancels before it standing."""
    platform = tenant.is_platform(session)
    for task_id, workspace_id in unsettled:
        _act_for(session, workspace_id)
        task = await session.get(Task, task_id)
        if task is not None:
            await _stop(session, task, admin)
    if platform:
        tenant.bind_platform(session)


async def _stop(session: AsyncSession, task: Task, admin: Actor) -> str:
    from prismoid.operations import steering  # layers: the one cancel sits a layer above this noun

    return await steering.cancel_task_row(session, task, admin)


async def unsuspend_user(session: AsyncSession, admin: Actor, user_id: uuid.UUID) -> tuple[User, Actor]:
    """Back on. Sessions and keys stay revoked - the person signs in again; nothing resurrects."""
    user, actor = await _target_actor(session, user_id)
    if actor.is_active():
        return user, actor
    actor.status = ActorStatus.ACTIVE.value
    audit(session, admin, "user.unsuspend", "user", str(user_id))
    await session.commit()
    return user, actor


async def grant_admin(session: AsyncSession, admin: Actor, user_id: uuid.UUID) -> tuple[User, Actor]:
    user, actor = await _target_actor(session, user_id)
    if not actor.is_active():
        raise ValidationFailed("cannot grant admin to a suspended account")
    if not actor.has_scope(Scope.ADMIN):
        # Reassigned, never mutated in place - JSONB change detection watches the reference.
        actor.scopes = [*actor.scopes, Scope.ADMIN.value]
        audit(session, admin, "admin.grant", "user", str(user_id))
        await session.commit()
    return user, actor


class Bootstrapped(NamedTuple):
    """What `bootstrap_admin` did: the account, whether it was made now, and the one-time
    set-password code (plaintext, only here and on the operator's terminal) when one was minted."""

    user: User
    created: bool
    code: str | None


async def bootstrap_admin(
    session: AsyncSession, *, email: str, name: str | None = None, new_code: bool = False
) -> Bootstrapped:
    """The first platform admin, made on the box (`prismoid admin bootstrap`), never through the web:
    signup does not verify an address, so an empty users table or a configured address would be
    claimed by whoever signs up first.

    An existing account gains the admin scope (idempotent). A new address gets its account now -
    with the scope, a personal workspace and a password nobody knows - and a password-reset `cr_`
    code (hashed at rest, TTL'd, single use) that its owner trades for a password at the reset
    door. `new_code` mints a fresh code for an existing account (the first one expired); minting
    supersedes any pending one. Commits."""
    from prismoid.operations import auth  # auth composes workspaces; this module stays a leaf of db

    try:  # the login door's own rule: an address it refuses would be an admin nobody can sign in as
        TypeAdapter(EmailStr).validate_python(email)
    except ValueError:  # pydantic ValidationError is one
        raise ValidationFailed(f"{email!r} is not an address login accepts", {"email": "invalid"}) from None
    user = await User.by_email(session, email)
    created = user is None
    if user is None:
        local = User.normalize_email(email).partition("@")[0]
        user, _ = await auth.create_account(
            session,
            email=email,
            name=(name or "").strip() or local,
            password_hash=auth.unusable_password_hash(),
            admin=True,
        )
    actor = await session.get(Actor, user.actor_id)
    if actor is None or not actor.is_active():
        raise ValidationFailed("cannot grant admin to a suspended account")
    if not actor.has_scope(Scope.ADMIN):
        actor.scopes = [*actor.scopes, Scope.ADMIN.value]  # reassigned: JSONB watches the reference
    code = None
    if created or new_code:
        await User.lock_row(session, user.actor_id)
        await PasswordReset.supersede_pending(session, user.actor_id)
        row, code = PasswordReset.issue(user.actor_id)
        session.add(row)
    await session.commit()
    logfire.info(
        "platform admin bootstrapped for user {user_id} (created={created}, code minted={minted})",
        user_id=str(user.actor_id),
        created=created,
        minted=code is not None,
    )
    return Bootstrapped(user=user, created=created, code=code)


async def revoke_admin(session: AsyncSession, admin: Actor, user_id: uuid.UUID) -> tuple[User, Actor]:
    """Remove the scope - unless this is the LAST active admin, which is refused outright.
    A system with zero admins has no way back but `prismoid admin bootstrap` on the box."""
    user, actor = await _target_actor(session, user_id)
    if not actor.has_scope(Scope.ADMIN):
        return user, actor
    if await Actor.admin_count(session) <= 1:
        raise ValidationFailed("this is the last admin - grant another before revoking this one")
    actor.scopes = [s for s in actor.scopes if s != Scope.ADMIN.value]
    audit(session, admin, "admin.revoke", "user", str(user_id))
    await session.commit()
    return user, actor


async def revoke_sessions(session: AsyncSession, admin: Actor, user_id: uuid.UUID) -> dict[str, int]:
    """Sign a user out everywhere: every live session and API key, one act, audited."""
    await _target_actor(session, user_id)  # 404 before side effects
    revoked_sessions = await AuthSession.revoke_all_for(session, user_id)
    revoked_keys = await ApiKey.revoke_all_for(session, user_id)
    audit(
        session,
        admin,
        "user.revoke_sessions",
        "user",
        str(user_id),
        {"sessions_revoked": revoked_sessions, "api_keys_revoked": revoked_keys},
    )
    await session.commit()
    return {"sessions_revoked": revoked_sessions, "api_keys_revoked": revoked_keys}


async def list_user_sessions(session: AsyncSession, user_id: uuid.UUID) -> list[AuthSession]:
    """Session METADATA for the Access tab - when/where/alive, never hashes. A read of
    metadata about credentials is dashboard, not intrusion (law 2's list-view line)."""
    await _target_actor(session, user_id)  # 404 before disclosure
    return await AuthSession.admin_list_for(session, user_id)


async def list_user_keys(session: AsyncSession, user_id: uuid.UUID) -> list[ApiKey]:
    await _target_actor(session, user_id)
    return await ApiKey.admin_list_for(session, user_id)


async def revoke_user_session(
    session: AsyncSession, admin: Actor, user_id: uuid.UUID, session_id: uuid.UUID
) -> bool:
    """Revoke ONE session - the surgical sibling of revoke_sessions, audited the same."""
    await _target_actor(session, user_id)
    revoked = await AuthSession.revoke_one(session, user_id, session_id)
    if revoked:
        audit(session, admin, "user.revoke_session", "user", str(user_id), {"session_id": str(session_id)})
        await session.commit()
    return revoked


async def revoke_user_key(session: AsyncSession, admin: Actor, user_id: uuid.UUID, key_id: uuid.UUID) -> bool:
    await _target_actor(session, user_id)
    revoked = await ApiKey.admin_revoke(session, user_id, key_id)
    if revoked:
        audit(session, admin, "user.revoke_api_key", "user", str(user_id), {"key_id": str(key_id)})
        await session.commit()
    return revoked


# ---------------------------------------------------------------- the task console


def stuck_reason(
    status: str,
    *,
    last_event_at: dt.datetime | None,
    updated_at: dt.datetime,
    held: bool,
    now: dt.datetime,
) -> str | None:
    """The row's one-line diagnosis, mirroring `Task.admin_stuck_filter` - a filter that flags a
    row the reason-line cannot explain is two definitions of stuck wearing one name."""
    if status in (TaskStatus.RUNNING.value, TaskStatus.WAITING.value):
        seen = last_event_at or updated_at
        quiet = (now - seen).total_seconds()
        if quiet >= STALE_EVENT_S:
            return f"{status} with no event for {int(quiet // 60)}m"
    elif (
        status
        in (
            TaskStatus.CREATED.value,
            TaskStatus.CLARIFYING.value,
            TaskStatus.PLANNING.value,
        )
        and not held
    ):
        age = (now - updated_at).total_seconds()
        if age >= UNDISPATCHED_S:
            return f"{status} for {int(age // 60)}m - never dispatched"
    return None


def _console_row(
    task: Task, owner: User | None, last_at: dt.datetime | None, spend: Any, now: dt.datetime
) -> dict[str, Any]:
    return {
        "task": task,
        "owner": owner,
        "last_event_at": last_at,
        "spend_usd": spend,
        "stuck_reason": stuck_reason(
            task.status,
            last_event_at=last_at,
            updated_at=task.updated_at,
            held=task.held_at is not None,
            now=now,
        ),
    }


async def list_tasks_admin(
    session: AsyncSession,
    *,
    statuses: list[str] | None = None,
    project_id: uuid.UUID | None = None,
    creator_id: uuid.UUID | None = None,
    stuck: bool = False,
    q: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> list[dict[str, Any]]:
    """The cross-user task list - metadata + owner + spend + the stuck diagnosis. No visibility
    filter, BY DESIGN (law 1): this is the operator's console, behind AdminDep, and it carries
    goal first-lines and owner identity - never prompts, transcripts, or artifact bytes."""
    now = utcnow()
    rows = await Task.admin_page(
        session,
        now=now,
        stale_s=STALE_EVENT_S,
        undispatched_s=UNDISPATCHED_S,
        statuses=statuses,
        project_id=project_id,
        creator_id=creator_id,
        stuck=stuck,
        q=q,
        limit=limit,
        cursor=cursor,
    )
    return [_console_row(t, u, at, spend, now) for t, u, at, spend in rows]


async def stuck_count(session: AsyncSession) -> int:
    return await Task.admin_stuck_count(
        session, now=utcnow(), stale_s=STALE_EVENT_S, undispatched_s=UNDISPATCHED_S
    )


async def get_task_admin(
    session: AsyncSession, admin: Actor, task_id: uuid.UUID, *, record_view: bool = True
) -> dict[str, Any]:
    """One task, whole-population fetch - and the fetch itself is AUDITED (law 2): opening
    another person's thread is an act the record must carry, not a silent read.
    `record_view=False` is for a mutation's own read-back (the cancel already wrote its row)."""
    row = await Task.admin_row(session, task_id)
    if row is None:
        raise NotFound("no such task")
    task, owner, last_at, spend = row
    if record_view:
        audit(session, admin, "task.view", "task", str(task_id))
        await session.commit()
    return _console_row(task, owner, last_at, spend, utcnow())


async def events_admin(
    session: AsyncSession, task_id: uuid.UUID, *, since: int = 0, limit: int = 500
) -> list[DurableEvent]:
    """The task's durable record, paged by seq - the SAME wire payloads the owner's stream
    carries, which is what makes the web's replay reducer reusable verbatim. The peek was
    audited at `get_task_admin`; paging the same thread is not a second act."""
    if await session.get(Task, task_id) is None:
        raise NotFound("no such task")
    return await TaskEvent.replay(session, task_id, since=since, limit=limit)


async def cancel_admin(session: AsyncSession, admin: Actor, task_id: uuid.UUID, *, reason: str) -> str:
    """The operator's kill switch for a runaway task - `steering.cancel_task_row`, the exact
    machinery of the user's own cancel (gates resolved, parks woken), reached through the
    audited admin fetch. The audit row (with the reason) rides the cancel's own commit.

    A finished task is a 409 here, where the user door treats it as a no-op - the MACHINERY
    must not fork (law: one cancel), but the policy may, and this door's policy is different.
    Silently no-op'ing meant the console toasted "Cancelled" over a task it had not touched,
    and the audit row died uncommitted with the early return: an operator was told an act
    happened, and the record - correctly - disagreed. Refusing says the true thing."""
    task = await session.get(Task, task_id)
    if task is None:
        raise NotFound("no such task")
    if task.is_terminal():
        raise Conflict(f"this task already finished ({task.status}) - there is nothing to cancel")
    _act_for(session, task.workspace_id)
    audit(session, admin, "task.cancel", "task", str(task_id), {"reason": reason, "was": task.status})
    status = await _stop(session, task, admin)
    logfire.info(
        "admin {admin} cancelled task {task}: {reason}",
        admin=str(admin.id),
        task=str(task_id),
        reason=reason,
    )
    return status


# ---------------------------------------------------------------- projects + bots (observation)


async def list_projects_admin(
    session: AsyncSession, *, q: str | None = None, limit: int = 50, cursor: str | None = None
) -> list[dict[str, Any]]:
    """Every project with its owner, size, and month-to-date spend vs budget - organizational
    observation, read-only (the owner's own settings stay the owner's; no verbs faked here)."""
    from prismoid.db.models.project import Project

    month_start = utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    rows = await Project.admin_page(session, month_start=month_start, q=q, limit=limit, cursor=cursor)
    return [{"project": sp, "owner": u, "members": m, "tasks": t, "mtd_usd": c} for sp, u, m, t, c in rows]


async def get_project_admin(session: AsyncSession, project_id: uuid.UUID) -> dict[str, Any]:
    from prismoid.db.models.project import Project
    from prismoid.db.models.project_member import ProjectMember

    month_start = utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    row = await Project.admin_row(session, project_id, month_start=month_start)
    if row is None:
        raise NotFound("no such project")
    sp, owner, members, tasks, mtd = row
    member_rows = await ProjectMember.for_project(session, project_id)
    users = await User.by_actor_ids(session, [m.actor_id for m in member_rows])
    return {
        "project": sp,
        "owner": owner,
        "members": members,
        "tasks": tasks,
        "mtd_usd": mtd,
        "member_rows": member_rows,
        "member_users": users,
    }


async def list_bots_admin(
    session: AsyncSession,
    *,
    status: str | None = None,
    q: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> list[dict[str, Any]]:
    """Every bot HEAD across the population (newest version per owner/project/name), any status."""
    from prismoid.db.models.bot import BotRecord

    rows = await BotRecord.admin_page(session, status=status, q=q, limit=limit, cursor=cursor)
    return [{"bot": b, "author": u} for b, u in rows]


async def get_bot_admin(session: AsyncSession, admin: Actor, bot_id: uuid.UUID) -> dict[str, Any]:
    """One bot, whole history - AUDITED like the task peek (law 2): instructions are an
    author's content, and reading them through the operator door is an act on the record."""
    from prismoid.db.models.bot import BotRecord

    row = await session.get(BotRecord, bot_id)
    if row is None:
        raise NotFound("no such bot")
    author = (await User.by_actor_ids(session, [row.owner_actor_id])).get(row.owner_actor_id)
    versions = await BotRecord.admin_versions(session, row.owner_actor_id, row.name, row.project_id)
    audit(session, admin, "bot.view", "bot", str(bot_id), {"name": row.name})
    await session.commit()
    return {"bot": row, "author": author, "versions": versions}


async def list_routines_admin(
    session: AsyncSession,
    *,
    status: str | None = None,
    q: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> list[dict[str, Any]]:
    """The standing-work population: every routine, any owner, newest first."""
    from prismoid.db.models.routine import Routine

    rows = await Routine.admin_page(session, status=status, q=q, limit=limit, cursor=cursor)
    return [{"routine": r, "owner": u} for r, u in rows]


async def pause_routine_admin(
    session: AsyncSession, admin: Actor, routine_id: uuid.UUID, *, reason: str
) -> None:
    """The operator's pause - audited in the SAME transaction (law: every admin mutation writes
    its admin_actions row in-tx), reason carried on the note AND to the owner's inbox: a pause
    you learn about from a dead schedule is an outage, not an intervention."""
    from prismoid.db.models.routine import Routine
    from prismoid.operations import inbox as inbox_ops
    from prismoid.schemas.routines import PausedReason, RoutineStatus

    routine = await session.get(Routine, routine_id)
    if routine is None:
        raise NotFound("no such routine")
    if routine.status == RoutineStatus.RETIRED:
        raise Conflict("a retired routine is already still")
    _act_for(session, routine.workspace_id)  # the owner's inbox item is born in the routine's workspace
    routine.status = RoutineStatus.PAUSED.value
    routine.paused_reason = PausedReason.ADMIN.value
    routine.pause_note = reason[:400]
    await inbox_ops.routine_paused(
        session,
        actor_id=routine.owner_actor_id,
        routine_id=routine.id,
        routine_name=routine.name,
        why=f"paused by an operator: {reason[:300]}",
        title=f"{routine.name} was paused by an operator",
    )
    audit(session, admin, "routine.pause", "routine", str(routine_id), {"reason": reason[:400]})
    await session.commit()


# ---------------------------------------------------------------- audited catalog wrappers


async def put_routing_audited(session: AsyncSession, admin: Actor, chains: dict[str, list[str]]) -> Any:
    from prismoid.operations import routing as route_ops

    audit(session, admin, "routing.put", "routing", "table", {"classes": sorted(chains)})
    return await route_ops.put_routing(session, admin.id, chains)


async def add_model_audited(session: AsyncSession, admin: Actor, **kwargs: Any) -> Any:
    from prismoid.operations import models as model_ops

    ref = f"{kwargs.get('provider')}:{kwargs.get('model_id')}"
    audit(session, admin, "model.add", "model", ref)
    return await model_ops.add_model(session, **kwargs)


async def patch_model_audited(
    session: AsyncSession,
    admin: Actor,
    model_ref: str,
    *,
    enabled: bool | None = None,
    classes: list[str] | None = None,
) -> Any:
    from prismoid.operations import models as model_ops

    changed: dict[str, Any] = {}
    if enabled is not None:
        changed["enabled"] = enabled
    if classes is not None:
        changed["classes"] = classes
    audit(session, admin, "model.patch", "model", model_ref, changed)
    return await model_ops.patch_model(session, model_ref, enabled=enabled, classes=classes)


async def put_prices_audited(session: AsyncSession, admin: Actor, prices: dict[str, dict[str, str]]) -> int:
    from prismoid.operations import prices as price_ops

    audit(session, admin, "prices.put", "prices", "book", {"refs": sorted(prices)})
    return await price_ops.put_book(session, prices)
