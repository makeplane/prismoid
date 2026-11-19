# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The workspace verbs (ADR-061). Operations own transactions.

`create_workspace` is the one way a workspace is born - the owner membership lands in the
caller's transaction, never a separate one, so a workspace never exists without its owner. `switch_workspace` moves a login session between the workspaces
its person belongs to; `my_workspaces` is the caller's roster of active memberships.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid

import logfire
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.conf import settings
from prismoid.db.models import tenant
from prismoid.db.models.actor import Actor, Scope
from prismoid.db.models.project import Project
from prismoid.db.models.session import AuthSession
from prismoid.db.models.user import User
from prismoid.db.models.workspace import Workspace, WorkspaceKind, WorkspaceMember, WorkspaceRole
from prismoid.db.models.workspace_invite import WorkspaceInvite
from prismoid.db.session import session_scope
from prismoid.errors import Conflict, Forbidden, NotFound, RateLimited, ValidationFailed, WorkspaceCapReached
from prismoid.operations.throttle import FixedWindowLimiter


async def create_workspace(
    session: AsyncSession, actor: Actor, *, name: str, kind: WorkspaceKind
) -> Workspace:
    """The workspace and its owner - flushed, NOT committed: signup folds it into its own
    transaction."""
    if not actor.is_human():
        raise Forbidden("a workspace is owned by a person")
    workspace = await Workspace.create(session, name=name, kind=kind, created_by=actor.id)
    await WorkspaceMember.add(session, workspace.id, actor.id, WorkspaceRole.OWNER)
    return workspace


_founding_limiter = FixedWindowLimiter("new workspaces")


async def can_found(session: AsyncSession, actor: Actor) -> bool:
    """May this person found another team workspace? Unset cap = yes; platform admins are exempt."""
    cap = settings.workspaces.max_founded
    if cap is None or actor.has_scope(Scope.ADMIN):
        return True
    return await Workspace.count_founded(session, actor.id) < cap


async def create_team_workspace(session: AsyncSession, actor: Actor, *, name: str) -> Workspace:
    """`POST /v1/workspaces`: a team workspace the caller owns - one commit. A person
    founds a few a minute: a speed bump against farming."""
    _founding_limiter.hit(actor.id, per_minute=settings.workspaces.create_rate_limit_per_minute)
    await User.lock_row(session, actor.id)  # two racing creates see each other's count
    if not await can_found(session, actor):
        raise WorkspaceCapReached("New workspaces are off here. Invite people into your workspace instead.")
    workspace = await create_workspace(session, actor, name=name, kind=WorkspaceKind.TEAM)
    await session.commit()
    await session.refresh(workspace)  # the grant bumped the balance in SQL
    return workspace


async def switch_workspace(
    session: AsyncSession, actor: Actor, auth_session_id: uuid.UUID, workspace_id: uuid.UUID
) -> Workspace:
    """Point this login session at another of the person's workspaces - every tab on the
    session follows. A non-member, an unknown workspace and a suspended workspace are one 404: existence is
    not leaked to a stranger."""
    member = await WorkspaceMember.get(session, workspace_id, actor.id)
    workspace = await Workspace.get(session, workspace_id)
    if member is None or workspace is None or not workspace.is_active():
        raise NotFound("no such workspace")
    await session.execute(
        update(AuthSession)
        .where(AuthSession.id == auth_session_id, AuthSession.user_id == actor.id)
        .values(active_workspace_id=workspace_id)
    )
    tenant.bind(session, workspace_id, member.role)
    await session.commit()
    return workspace


async def my_workspaces(session: AsyncSession, actor: Actor) -> list[tuple[Workspace, WorkspaceMember]]:
    """The caller's memberships in active workspaces, earliest first (the personal workspace leads)."""
    return [
        (workspace, m)
        for workspace, m in await Workspace.for_actor(session, actor.id)
        if workspace.is_active()
    ]


# ── membership, roles, invites, transfer ──────────────────────────────────────────────────────

_SLUG = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")


async def require_role(
    session: AsyncSession, workspace_id: uuid.UUID, actor: Actor, minimum: WorkspaceRole
) -> tuple[Workspace, WorkspaceMember]:
    """The Project.get_for split: a non-member (or an unknown/suspended workspace) is a 404 - existence
    is not leaked - and a member below `minimum` is a 403. A credential pinned to another workspace
    (an API key) is a stranger here too, whatever its person's role."""
    pinned = tenant.pinned_workspace(session)
    if pinned is not None and pinned != workspace_id:
        raise NotFound("no such workspace")
    member = await WorkspaceMember.get(session, workspace_id, actor.id)
    workspace = await Workspace.get(session, workspace_id)
    if member is None or workspace is None or not workspace.is_active():
        raise NotFound("no such workspace")
    if member.workspace_role.rank < minimum.rank:
        raise Forbidden(f"this needs the {minimum.value} role")
    return workspace, member


async def _guard_last_owner(session: AsyncSession, workspace_id: uuid.UUID, target: WorkspaceMember) -> None:
    """A workspace is never ownerless: called before demoting, removing or leaving an owner."""
    if (
        target.workspace_role is WorkspaceRole.OWNER
        and await WorkspaceMember.owner_count(session, workspace_id) <= 1
    ):
        raise Conflict("a workspace needs an owner")


async def get_workspace(
    session: AsyncSession, actor: Actor, workspace_id: uuid.UUID
) -> tuple[Workspace, WorkspaceRole]:
    workspace, member = await require_role(session, workspace_id, actor, WorkspaceRole.GUEST)
    return workspace, member.workspace_role


async def rename_workspace(
    session: AsyncSession, actor: Actor, workspace_id: uuid.UUID, *, name: str | None, slug: str | None
) -> tuple[Workspace, WorkspaceRole]:
    workspace, member = await require_role(session, workspace_id, actor, WorkspaceRole.OWNER)
    if name is not None:
        workspace.name = Workspace.clean_name(name)
    if slug is not None:
        if not _SLUG.fullmatch(slug):
            raise ValidationFailed("invalid slug", {"slug": "lowercase letters, digits and dashes"})
        if await Workspace.slug_taken(session, slug, except_id=workspace.id):
            raise Conflict("that slug is taken")
        workspace.slug = slug
    await session.commit()
    return workspace, member.workspace_role


async def list_members(
    session: AsyncSession, actor: Actor, workspace_id: uuid.UUID
) -> list[tuple[WorkspaceMember, User]]:
    """Member and above see the roster; a guest sees only themselves."""
    _, me = await require_role(session, workspace_id, actor, WorkspaceRole.GUEST)
    rows = (
        [me]
        if me.workspace_role is WorkspaceRole.GUEST
        else await WorkspaceMember.roster(session, workspace_id)
    )
    users = await User.by_actor_ids(session, [m.actor_id for m in rows])
    return [(m, users[m.actor_id]) for m in rows if m.actor_id in users]


async def _target(session: AsyncSession, workspace_id: uuid.UUID, actor_id: uuid.UUID) -> WorkspaceMember:
    target = await WorkspaceMember.get(session, workspace_id, actor_id)
    if target is None:
        raise NotFound("no such member")
    return target


async def set_role(
    session: AsyncSession, actor: Actor, workspace_id: uuid.UUID, target_id: uuid.UUID, role: WorkspaceRole
) -> tuple[WorkspaceMember, User]:
    await Workspace.lock_row(session, workspace_id)  # serialize the owner-count guard
    _, me = await require_role(session, workspace_id, actor, WorkspaceRole.ADMIN)
    target = await _target(session, workspace_id, target_id)
    if role is not WorkspaceRole.OWNER:
        await _guard_last_owner(session, workspace_id, target)
    if WorkspaceRole.OWNER in (role, target.workspace_role) and me.workspace_role is not WorkspaceRole.OWNER:
        raise Forbidden("owner roles are changed by owners")
    target.role = role.value
    await session.commit()
    user = await User.by_actor_ids(session, [target_id])
    return target, user[target_id]


async def authorize_removal(
    session: AsyncSession, actor: Actor, workspace_id: uuid.UUID, target_id: uuid.UUID
) -> WorkspaceMember:
    """Everything `remove_member` refuses on, with nothing written. Takes the workspace row lock and
    leaves it held (the caller's commit or rollback frees it); `departures` asks it first, so a removal
    that will be refused never cancels anyone's work."""
    await Workspace.lock_row(session, workspace_id)  # serialize the owner-count guard
    _, me = await require_role(session, workspace_id, actor, WorkspaceRole.ADMIN)
    target = await _target(session, workspace_id, target_id)
    await _guard_last_owner(session, workspace_id, target)
    if target.workspace_role is WorkspaceRole.OWNER and me.workspace_role is not WorkspaceRole.OWNER:
        raise Forbidden("owner roles are changed by owners")
    return target


async def remove_member(
    session: AsyncSession, actor: Actor, workspace_id: uuid.UUID, target_id: uuid.UUID
) -> None:
    await authorize_removal(session, actor, workspace_id, target_id)
    await _hand_over_projects(session, workspace_id, target_id, heir=actor.id)
    await WorkspaceMember.remove(session, workspace_id, target_id)
    await session.commit()


async def authorize_leave(session: AsyncSession, actor: Actor, workspace_id: uuid.UUID) -> uuid.UUID:
    """Everything `leave_workspace` refuses on, with nothing written; returns the heir of the leaver's
    shared Projects. Leaves the workspace and membership row locks held, like `authorize_removal`."""
    await Workspace.lock_row(session, workspace_id)  # serialize the owner-count guard
    _, me = await require_role(session, workspace_id, actor, WorkspaceRole.GUEST)
    await _guard_last_owner(session, workspace_id, me)
    if await WorkspaceMember.lock_active_for(session, actor.id) <= 1:
        raise Conflict("you cannot leave your only workspace - create or join another first")
    return await WorkspaceMember.first_owner(session, workspace_id, besides=actor.id)


async def leave_workspace(session: AsyncSession, actor: Actor, workspace_id: uuid.UUID) -> None:
    """Leave a workspace - never the last owner, and never the person's last active membership: with
    none left they could act nowhere but the way back in."""
    heir = await authorize_leave(session, actor, workspace_id)
    await _hand_over_projects(session, workspace_id, actor.id, heir=heir)
    await WorkspaceMember.remove(session, workspace_id, actor.id)
    await session.commit()


async def _hand_over_projects(
    session: AsyncSession, workspace_id: uuid.UUID, departing: uuid.UUID, *, heir: uuid.UUID
) -> None:
    """A departing person's SHARED Projects in the workspace pass to `heir` (the admin who removed them,
    or the workspace's owner when they leave): workspace roles grant nothing inside a Project, so a
    shared Project whose owner can no longer act in the workspace could never be managed again.

    Their PRIVATE Projects (no members) are archived, never handed over: removing someone must not
    give their private work to the admin. The Project stays
    theirs, so no one in the workspace can open it or its tasks while they are out, an admin or owner
    included, and it is theirs again if they rejoin."""
    moved = await Project.hand_over(session, workspace_id, departing, heir)
    if moved:
        logfire.info(
            "{n} project(s) of {departing} handed to {heir} in workspace {workspace}",
            n=len(moved),
            departing=str(departing),
            heir=str(heir),
            workspace=str(workspace_id),
        )
    archived = await Project.count_owned(session, workspace_id, departing)
    if archived:
        logfire.info(
            "{n} private project(s) of {departing} archived in workspace {workspace}",
            n=archived,
            departing=str(departing),
            workspace=str(workspace_id),
        )


async def transfer_ownership(
    session: AsyncSession, actor: Actor, workspace_id: uuid.UUID, to_actor_id: uuid.UUID
) -> Workspace:
    """The owner makes another member an owner and steps down to admin, in one commit."""
    await Workspace.lock_row(session, workspace_id)  # serialize the owner-count guard
    workspace, me = await require_role(session, workspace_id, actor, WorkspaceRole.OWNER)
    if to_actor_id == actor.id:
        raise ValidationFailed("transfer to someone else", {"actor_id": "that is you"})
    target = await _target(session, workspace_id, to_actor_id)
    target.role = WorkspaceRole.OWNER.value
    me.role = WorkspaceRole.ADMIN.value
    await session.commit()
    return workspace


# The invite door's refusal for a person whose own address is not proven yet (ADR-086).
UNVERIFIED_INVITER = "Verify your email before you invite anyone."


async def invite(
    session: AsyncSession, actor: Actor, workspace_id: uuid.UUID, *, email: str, role: WorkspaceRole
) -> WorkspaceInvite:
    """Mint one live invite and mail it after commit (the password-reset shape). An owner is made by
    transfer, never by invite.

    The mail leaves from our domain with a workspace name and an inviter name the sender chose, so
    with open signup it is guarded like the reset (ADR-086): a person sends invites only once their
    own address is verified, and at most `invite_max_per_hour` an hour."""
    workspace, _ = await require_role(session, workspace_id, actor, WorkspaceRole.ADMIN)
    if role is WorkspaceRole.OWNER:
        raise ValidationFailed("an owner is made by transfer", {"role": "invite as admin, member or guest"})
    inviter = await session.get(User, actor.id)
    if inviter is not None and inviter.email_verified_at is None:
        raise Forbidden(UNVERIFIED_INVITER)
    email = User.normalize_email(email)
    existing = await User.by_email(session, email)
    if (
        existing is not None
        and await WorkspaceMember.get(session, workspace_id, existing.actor_id) is not None
    ):
        raise Conflict("already a member")
    # Serialize concurrent mints on the workspace row: under READ COMMITTED two interleaved mints
    # each supersede nothing and both stay live.
    await Workspace.lock_row(session, workspace_id)
    # Counted under the workspace lock: concurrent invites into one workspace cannot both slip under.
    if await WorkspaceInvite.recent_count_by(session, actor.id, dt.timedelta(hours=1)) >= (
        settings.workspaces.invite_max_per_hour
    ):
        raise RateLimited("too many invites this hour; try again later", retry_after=3600)
    row, code = await WorkspaceInvite.mint(session, workspace_id, email, role, actor.id)
    await session.commit()
    from prismoid.operations.auth import send_in_background  # auth imports this module

    who = inviter.name if inviter is not None else "Someone"
    send_in_background(
        _deliver_invite(row.id, workspace_id, to=email, inviter=who, workspace_name=workspace.name, code=code)
    )
    return row


async def _deliver_invite(
    invite_id: uuid.UUID, workspace_id: uuid.UUID, *, to: str, inviter: str, workspace_name: str, code: str
) -> None:
    """Send, and if the send failed, un-mint: an undeliverable code is retired, never left live."""
    from prismoid import mailer

    sent = await mailer.send(
        to=to,
        subject=f"You are invited to {workspace_name} on Prismoid",
        text=(
            f"{inviter} invited you to join {workspace_name}.\n"
            f"\n"
            f"Your one-time invite code:\n"
            f"\n"
            f"    {code}\n"
            f"\n"
            f"New to Prismoid? Sign up with this email address and paste the code into the\n"
            f"Invite code field.\n"
            f"Already have an account? Log in, open the workspace menu at the top of the\n"
            f"sidebar, choose Join with a code, and paste it there.\n"
            f"\n"
            f"It works once and expires in {settings.workspaces.invite_ttl_days} days."
        ),
    )
    if sent:
        return
    async with session_scope(workspace_id=workspace_id) as session:
        await WorkspaceInvite.retire(session, invite_id)
        await session.commit()


async def list_invites(session: AsyncSession, actor: Actor, workspace_id: uuid.UUID) -> list[WorkspaceInvite]:
    await require_role(session, workspace_id, actor, WorkspaceRole.ADMIN)
    return await WorkspaceInvite.live_for_workspace(session, workspace_id)


async def revoke_invite(
    session: AsyncSession, actor: Actor, workspace_id: uuid.UUID, invite_id: uuid.UUID
) -> None:
    await require_role(session, workspace_id, actor, WorkspaceRole.ADMIN)
    row = await WorkspaceInvite.get_live(session, workspace_id, invite_id)
    if row is None:
        raise NotFound("no such invite")
    await WorkspaceInvite.revoke(session, row)
    await session.commit()


async def check_invite(session: AsyncSession, email: str, code: str) -> WorkspaceInvite:
    """The live invite for this code, for THIS address only: another address is a 403 and the
    invite stays pending (it is the invitee's, not the finder's). Reads only, so signup asks it
    before it creates any row."""
    row = await WorkspaceInvite.by_code(session, code)
    if row is None:
        raise NotFound("this invite is invalid or has expired")
    if User.normalize_email(email) != row.email:
        raise Forbidden("this invite was sent to a different email address")
    return row


async def join_by_invite(session: AsyncSession, user: User, row: WorkspaceInvite) -> Workspace:
    """Spend the invite and add the membership - flushed, NOT committed (signup folds it in)."""
    workspace = await Workspace.get(session, row.workspace_id)
    if workspace is None or not workspace.is_active():
        raise NotFound("this invite is invalid or has expired")
    if await WorkspaceMember.get(session, workspace.id, user.actor_id) is not None:
        raise Conflict("already a member")
    if not await WorkspaceInvite.consume(session, row.id):
        raise NotFound("this invite is invalid or has expired")
    await WorkspaceMember.add(
        session, workspace.id, user.actor_id, WorkspaceRole(row.role), row.invited_by_actor_id
    )
    return workspace


async def accept_invite(
    session: AsyncSession, actor: Actor, code: str, auth_session_id: uuid.UUID | None
) -> tuple[Workspace, WorkspaceRole]:
    user = await session.get(User, actor.id)
    if user is None:
        raise NotFound("no such user")
    row = await check_invite(session, user.email, code)
    tenant.bind(session, row.workspace_id, row.role)  # the invite's workspace, not the caller's active one
    workspace = await join_by_invite(session, user, row)
    if auth_session_id is not None:
        await session.execute(
            update(AuthSession)
            .where(AuthSession.id == auth_session_id, AuthSession.user_id == actor.id)
            .values(active_workspace_id=workspace.id)
        )
    await session.commit()
    return workspace, WorkspaceRole(row.role)
