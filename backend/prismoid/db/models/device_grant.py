# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""device_grants - what a person granted a task on the device: a folder,
read-write or read-only, or Docker. Postgres is the authority; the worker tells the helper at a
frame's create and bind, and only a person grants (`--folder` at create, `prismoid device grants
add`), never a model.

A tenant root, pinned to its task's workspace by the composite `(task_id, workspace_id)` FK.
`path` is the folder as the person granted it, resolved: their own input, and the one place a real
path of their disk is stored - a frame spells it `/work` or `/grants/<name>`, never really. A
task's first live read-write folder is its work folder. Revoke is soft, so the history stays.
A folder belongs to a disk, not a workspace: "is it busy" alone looks across workspaces.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Final

from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, Index, String, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from prismoid.db.base import Base, StrEnumColumn, created_at, escape_like, uuid_pk
from prismoid.db.models import tenant
from prismoid.db.models.task import TERMINAL, Task
from prismoid.errors import NotFound
from prismoid.schemas.device_grants import FolderHolder, GrantKind, GrantMode, key_ancestors

# One lock for the whole deployment (`lock_folders`). A constant, so it names no tenant.
FOLDERS_LOCK: Final = "device_folders"


class DeviceGrant(Base):
    __tablename__ = "device_grants"
    __table_args__ = (
        ForeignKeyConstraint(
            ["task_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            name="fk_device_grants_task_workspace",
            ondelete="CASCADE",
        ),
        Index("ix_device_grants_task_id", "task_id"),
        # A folder's virtual root is the task's for good: never two rows, revoked ones included.
        Index("ux_device_grants_task_name", "task_id", "name", unique=True),
        Index("ix_device_grants_path_key", "path_key"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    task_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    kind: Mapped[GrantKind] = mapped_column(StrEnumColumn(GrantKind, 10), nullable=False)
    path: Mapped[str | None] = mapped_column(String(1024), default=None)  # a folder's; NULL for docker
    mode: Mapped[GrantMode | None] = mapped_column(StrEnumColumn(GrantMode, 2), default=None)  # docker: NULL
    # A folder's virtual root (`/work`, `/grants/<name>`), minted once when the grant is made
    # (`schemas.device_grants.mint_grant_name`) and never reused in the task; NULL for docker.
    name: Mapped[str | None] = mapped_column(String(80), default=None)
    # `schemas.device_grants.folder_key(path)`: what "is this folder busy" compares, computed in Python -
    # SQL never folds case (local mode's Postgres is C-collated: its lower() is ASCII only).
    path_key: Mapped[str | None] = mapped_column(String(2048), default=None)
    created_by_actor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("actors.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[dt.datetime] = created_at()
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    @classmethod
    async def live_for_task(cls, session: AsyncSession, task_id: uuid.UUID) -> list[DeviceGrant]:
        """The task's live grants, oldest first: the first read-write folder is its work folder."""
        query = (
            select(cls)
            .where(
                cls.workspace_id == tenant.workspace_of(session),
                cls.task_id == task_id,
                cls.revoked_at.is_(None),
            )
            .order_by(cls.created_at, cls.id)
        )
        return list(await session.scalars(query))

    @classmethod
    async def version_of(cls, session: AsyncSession, task_id: uuid.UUID) -> tuple[int, int]:
        """How many grants the task ever had, and how many are revoked. No row is removed or revived,
        so the pair moves with every grant and revoke, and no clock is compared. One read of
        `ix_device_grants_task_id`, cheap enough for a step's every tool dispatch."""
        query = select(func.count(cls.id), func.count(cls.revoked_at)).where(
            cls.workspace_id == tenant.workspace_of(session), cls.task_id == task_id
        )
        granted, revoked = (await session.execute(query)).one()
        return granted, revoked

    @staticmethod
    async def lock_folders(session: AsyncSession) -> None:
        """Serialize every folder claim of this deployment until the transaction ends: a claim is
        checked against every folder above and below it, so one lock per folder would not do, and a
        deployment's device is one computer whichever workspace claims, so one per workspace would
        not either."""
        await session.execute(select(func.pg_advisory_xact_lock(func.hashtext(FOLDERS_LOCK))))

    @classmethod
    async def all_names(cls, session: AsyncSession, task_id: uuid.UUID) -> list[str]:
        """Every virtual root the task's grants ever had, revoked ones included."""
        query = select(cls.name).where(
            cls.workspace_id == tenant.workspace_of(session), cls.task_id == task_id, cls.name.is_not(None)
        )
        return [str(n) for n in await session.scalars(query)]

    @classmethod
    async def folder_holder(
        cls, session: AsyncSession, key: str, *, other_than: uuid.UUID | None = None
    ) -> FolderHolder | None:
        """The oldest live task (not settled) holding read-write the folder whose `folder_key` is
        `key`, one inside it or one around it - on a path boundary (`/a/bc` is not inside `/a/b`) -
        but `other_than`; the folder it holds, and whether that grant was revoked. A grant revoked
        from a task still live holds its folder too: a command it started before keeps writing there.
        Keys only: the ancestors are listed in Python, the descendants matched by an escaped prefix.

        The one tenant read that crosses workspaces, on purpose: a deployment's device is one
        computer, so a folder is busy in every workspace on it. It still needs a door's
        workspace bound, and hands back a value: no other workspace's row enters this session."""
        tenant.workspace_of(session)
        query = (
            select(
                Task.id,
                Task.workspace_id,
                Task.creator_actor_id,
                Task.title,
                Task.goal,
                cls.path,
                cls.revoked_at,
            )
            .join(cls, and_(cls.task_id == Task.id, cls.workspace_id == Task.workspace_id))
            .where(
                cls.kind == GrantKind.FOLDER,
                cls.mode == GrantMode.RW,
                or_(
                    cls.path_key == key,
                    cls.path_key.in_(key_ancestors(key)),
                    cls.path_key.like(f"{escape_like(key)}/%", escape="\\"),
                ),
                Task.status.not_in([s.value for s in TERMINAL]),
            )
            .order_by(Task.created_at)
            .limit(1)
        )
        if other_than is not None:
            query = query.where(Task.id != other_than)
        row = (await session.execute(query)).first()
        if row is None:
            return None
        return FolderHolder(
            task_id=row.id,
            workspace_id=row.workspace_id,
            creator_actor_id=row.creator_actor_id,
            label=row.title or row.goal,
            path=str(row.path),
            revoked=row.revoked_at is not None,
        )

    @classmethod
    async def live_of_creator(
        cls, session: AsyncSession, actor_id: uuid.UUID, task_id: uuid.UUID | None = None
    ) -> list[DeviceGrant]:
        """The live grants of the tasks `actor_id` created (one task's, given one), oldest first."""
        query = (
            select(cls)
            .join(Task, and_(Task.id == cls.task_id, Task.workspace_id == cls.workspace_id))
            .where(
                cls.workspace_id == tenant.workspace_of(session),
                cls.revoked_at.is_(None),
                Task.creator_actor_id == actor_id,
            )
            .order_by(cls.created_at, cls.id)
        )
        if task_id is not None:
            query = query.where(cls.task_id == task_id)
        return list(await session.scalars(query))

    @classmethod
    async def get_for_creator(
        cls, session: AsyncSession, grant_id: uuid.UUID, actor_id: uuid.UUID
    ) -> DeviceGrant:
        """A grant of a task `actor_id` created; anyone else's is `NotFound`, as one that never was."""
        row = await session.scalar(
            select(cls)
            .join(Task, and_(Task.id == cls.task_id, Task.workspace_id == cls.workspace_id))
            .where(
                cls.id == grant_id,
                cls.workspace_id == tenant.workspace_of(session),
                Task.creator_actor_id == actor_id,
            )
        )
        if row is None:
            raise NotFound("no such grant")
        return row
