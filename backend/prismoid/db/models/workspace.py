# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""workspaces / workspace_members - the tenant (ADR-061) and who belongs to it. A person may belong
to many; a session works in one at a time. Owners are humans; a workspace is never ownerless."""

from __future__ import annotations

import datetime as dt
import re
import secrets
import uuid
from enum import StrEnum

from sqlalchemy import ForeignKey, String, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from prismoid.db.base import Base, created_at, keyset_before, uuid_pk, vocabulary
from prismoid.db.models.task import Task
from prismoid.errors import Conflict, ValidationFailed
from prismoid.schemas.task_states import TERMINAL


class WorkspaceRole(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    GUEST = "guest"

    @property
    def rank(self) -> int:
        return {"owner": 3, "admin": 2, "member": 1, "guest": 0}[self.value]

    @property
    def sees_emails(self) -> bool:
        """A guest knows the workspace's people by name only. The workspace roster shows a guest only
        themselves (`operations.workspaces.list_members`), so no other surface may hand them anyone's
        email either - a Project they joined names its owner and members without one. Members
        and up see emails, as they do in the roster."""
        return self.rank >= WorkspaceRole.MEMBER.rank


class WorkspaceKind(StrEnum):
    PERSONAL = "personal"
    TEAM = "team"


class WorkspaceStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


def slugify(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "workspace"
    return f"{base}-{secrets.token_hex(3)}"


class Workspace(Base):
    __tablename__ = "workspaces"
    __table_args__ = (vocabulary("kind", WorkspaceKind), vocabulary("status", WorkspaceStatus))

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=WorkspaceStatus.ACTIVE.value)
    created_by_actor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("actors.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[dt.datetime] = created_at()

    def is_active(self) -> bool:
        return self.status == WorkspaceStatus.ACTIVE.value

    @staticmethod
    def clean_name(name: str) -> str:
        """A name is what is left after the whitespace: an all-blank one passes a length check
        and strips to nothing, so it is refused here, after stripping."""
        cleaned = name.strip()[:120]
        if not cleaned:
            raise ValidationFailed("a workspace needs a name", {"name": "blank"})
        return cleaned

    @classmethod
    async def create(
        cls, session: AsyncSession, *, name: str, kind: WorkspaceKind, created_by: uuid.UUID
    ) -> Workspace:
        cleaned = cls.clean_name(name)
        workspace = cls(name=cleaned, slug=slugify(cleaned), kind=kind.value, created_by_actor_id=created_by)
        session.add(workspace)
        await session.flush()
        return workspace

    @classmethod
    async def get(cls, session: AsyncSession, workspace_id: uuid.UUID) -> Workspace | None:
        return await session.get(cls, workspace_id)

    @classmethod
    async def personal_of(cls, session: AsyncSession, actor_id: uuid.UUID) -> Workspace | None:
        """The workspace made for this person at signup: the one personal workspace they created."""
        return await session.scalar(
            select(cls).where(cls.created_by_actor_id == actor_id, cls.kind == WorkspaceKind.PERSONAL.value)
        )

    @classmethod
    async def count_founded(cls, session: AsyncSession, actor_id: uuid.UUID) -> int:
        """Every team workspace this actor created, in any status: a suspended one was still founded."""
        return (
            await session.scalar(
                select(func.count())
                .select_from(cls)
                .where(cls.created_by_actor_id == actor_id, cls.kind == WorkspaceKind.TEAM.value)
            )
            or 0
        )

    @classmethod
    async def lock_row(cls, session: AsyncSession, workspace_id: uuid.UUID) -> None:
        """`FOR UPDATE` on the workspace row, held to commit - the serialization point for per-workspace
        invariants that span statements (one live invite per (workspace, email))."""
        await session.execute(select(cls.id).where(cls.id == workspace_id).with_for_update())

    @classmethod
    async def slug_taken(cls, session: AsyncSession, slug: str, *, except_id: uuid.UUID) -> bool:
        return (
            await session.scalar(select(cls.id).where(cls.slug == slug, cls.id != except_id).limit(1))
        ) is not None

    @classmethod
    async def for_actor(
        cls, session: AsyncSession, actor_id: uuid.UUID
    ) -> list[tuple[Workspace, WorkspaceMember]]:
        rows = await session.execute(
            select(cls, WorkspaceMember)
            .join(WorkspaceMember, WorkspaceMember.workspace_id == cls.id)
            .where(WorkspaceMember.actor_id == actor_id)
            .order_by(WorkspaceMember.created_at, cls.id)
        )
        return [(workspace, member) for workspace, member in rows.all()]

    @classmethod
    async def admin_page(
        cls, session: AsyncSession, *, limit: int, cursor: str | None
    ) -> list[tuple[Workspace, int]]:
        """The operator's list: every workspace (platform mode), newest first, with its member count."""
        members = (
            select(WorkspaceMember.workspace_id, func.count().label("n"))
            .group_by(WorkspaceMember.workspace_id)
            .subquery()
        )
        query = (
            select(cls, func.coalesce(members.c.n, 0))
            .outerjoin(members, members.c.workspace_id == cls.id)
            .order_by(cls.created_at.desc(), cls.id.desc())
            .limit(limit)
        )
        if cursor is not None:
            query = query.where(keyset_before(cls, cursor))
        return [(workspace, int(n)) for workspace, n in (await session.execute(query)).all()]

    @classmethod
    async def member_count(cls, session: AsyncSession, workspace_id: uuid.UUID) -> int:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(WorkspaceMember)
                .where(WorkspaceMember.workspace_id == workspace_id)
            )
            or 0
        )


class WorkspaceMember(Base):
    __tablename__ = "workspace_members"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    actor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("actors.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    invited_by_actor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("actors.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[dt.datetime] = created_at()

    @property
    def workspace_role(self) -> WorkspaceRole:
        return WorkspaceRole(self.role)

    @classmethod
    async def get(
        cls, session: AsyncSession, workspace_id: uuid.UUID, actor_id: uuid.UUID
    ) -> WorkspaceMember | None:
        return await session.get(cls, (workspace_id, actor_id))

    @classmethod
    async def add(
        cls,
        session: AsyncSession,
        workspace_id: uuid.UUID,
        actor_id: uuid.UUID,
        role: WorkspaceRole,
        invited_by: uuid.UUID | None = None,
    ) -> WorkspaceMember:
        row = cls(
            workspace_id=workspace_id, actor_id=actor_id, role=role.value, invited_by_actor_id=invited_by
        )
        session.add(row)
        await session.flush()
        return row

    @classmethod
    async def remove(cls, session: AsyncSession, workspace_id: uuid.UUID, actor_id: uuid.UUID) -> None:
        await session.execute(delete(cls).where(cls.workspace_id == workspace_id, cls.actor_id == actor_id))

    @classmethod
    async def live_tasks(
        cls, session: AsyncSession, workspace_id: uuid.UUID, actor_id: uuid.UUID
    ) -> list[Task]:
        """What a departure stops: the member's non-terminal tasks (running, queued, parked, held) in this
        workspace - the ones they created, and the ones whose current turn is theirs (it spends for them)."""
        mine = or_(Task.creator_actor_id == actor_id, Task.turn_actor_id == actor_id)
        live = Task.status.not_in([s.value for s in TERMINAL])
        return list(await session.scalars(select(Task).where(Task.workspace_id == workspace_id, mine, live)))

    @classmethod
    async def earliest_for(cls, session: AsyncSession, actor_id: uuid.UUID) -> WorkspaceMember | None:
        return await session.scalar(
            select(cls)
            .join(Workspace, Workspace.id == cls.workspace_id)
            .where(cls.actor_id == actor_id, Workspace.status == WorkspaceStatus.ACTIVE.value)
            .order_by(cls.created_at, cls.workspace_id)
            .limit(1)
        )

    @classmethod
    async def lock_active_for(cls, session: AsyncSession, actor_id: uuid.UUID) -> int:
        """How many active workspaces the person belongs to, with those membership rows locked: two
        leaves by one person serialize here (in workspace order, so never in a deadlock), and the later
        one counts what the earlier left behind."""
        rows = await session.scalars(
            select(cls.workspace_id)
            .join(Workspace, Workspace.id == cls.workspace_id)
            .where(cls.actor_id == actor_id, Workspace.status == WorkspaceStatus.ACTIVE.value)
            .order_by(cls.workspace_id)
            .with_for_update(of=cls)
        )
        return len(rows.all())

    @classmethod
    async def owner_count(cls, session: AsyncSession, workspace_id: uuid.UUID) -> int:
        return (
            await session.scalar(
                select(func.count())
                .select_from(cls)
                .where(cls.workspace_id == workspace_id, cls.role == WorkspaceRole.OWNER.value)
            )
            or 0
        )

    @classmethod
    async def first_owner(
        cls, session: AsyncSession, workspace_id: uuid.UUID, *, besides: uuid.UUID
    ) -> uuid.UUID:
        """The longest-standing owner other than `besides` - there is one whenever `besides` may
        go (a workspace is never ownerless, and the last owner cannot leave)."""
        owner = await session.scalar(
            select(cls.actor_id)
            .where(
                cls.workspace_id == workspace_id,
                cls.role == WorkspaceRole.OWNER.value,
                cls.actor_id != besides,
            )
            .order_by(cls.created_at, cls.actor_id)
            .limit(1)
        )
        if owner is None:
            raise Conflict("a workspace needs an owner")
        return owner

    @classmethod
    async def roster(cls, session: AsyncSession, workspace_id: uuid.UUID) -> list[WorkspaceMember]:
        return list(
            await session.scalars(
                select(cls).where(cls.workspace_id == workspace_id).order_by(cls.created_at, cls.actor_id)
            )
        )
