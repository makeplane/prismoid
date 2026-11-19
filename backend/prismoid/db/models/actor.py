# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Actor - the thin attribution principal.

Everything attributable in the system points here, and there is exactly one such spine: humans,
agents and services share it, so attribution never forks. The row is deliberately *thin* -
kind, name, scopes, status - because the moment an Actor grows a profile it stops being an
attribution principal and starts being a User, which is a different table with a different
lifecycle (GDPR erasure applies to one and not the other).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterable
from enum import StrEnum

from sqlalchemy import Index, String, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from prismoid.db.base import Base, created_at, uuid_pk, vocabulary


class ActorKind(StrEnum):
    HUMAN = "human"
    AGENT = "agent"  # one per ai/agents registry entry, upserted at worker boot
    SERVICE = "service"


class ActorStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"  # the kill switch - which is why sessions are opaque, not JWT


class Scope(StrEnum):
    """RBAC, deliberately coarse. `/admin/*` checks for ADMIN in require_actor."""

    ADMIN = "admin"


class Actor(Base):
    __tablename__ = "actors"
    __table_args__ = (
        vocabulary("kind", ActorKind),
        vocabulary("status", ActorStatus),
        # One agent Actor per registry name. `sync_agents` runs on every worker boot, and two
        # workers booting at the same instant both read-then-inserted - a race that minted twin
        # agents (audit finding). This partial unique index makes the twin a constraint error and
        # lets the insert use ON CONFLICT DO NOTHING; only kind='agent' rows carry the rule.
        Index(
            "uq_actors_agent_display_name",
            "display_name",
            unique=True,
            postgresql_where=text("kind = 'agent'"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=ActorStatus.ACTIVE)
    created_at: Mapped[dt.datetime] = created_at()

    def is_active(self) -> bool:
        return self.status == ActorStatus.ACTIVE

    def has_scope(self, scope: Scope) -> bool:
        return scope.value in self.scopes

    def is_human(self) -> bool:
        return self.kind == ActorKind.HUMAN

    @classmethod
    async def names(cls, session: AsyncSession, ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, str]:
        """Display names for many actors in one query; an id with no row is absent from the answer."""
        wanted = set(ids)
        if not wanted:
            return {}
        rows = await session.execute(select(cls.id, cls.display_name).where(cls.id.in_(wanted)))
        return dict(rows.tuples().all())

    @classmethod
    async def admin_count(cls, session: AsyncSession) -> int:
        """How many ACTIVE humans hold the admin scope - the last-admin guard's read (admin
        surface): revoking the only admin would leave the system reachable by SQL alone."""
        from sqlalchemy import func, select

        return int(
            await session.scalar(
                select(func.count()).where(
                    cls.kind == ActorKind.HUMAN.value,
                    cls.status == ActorStatus.ACTIVE.value,
                    cls.scopes.contains([Scope.ADMIN.value]),
                )
            )
            or 0
        )

    @classmethod
    async def sync_agents(cls, session: AsyncSession, names: list[str]) -> int:
        """One `kind=agent` Actor per registry entry, upserted at worker boot.

        Machine attribution is uniform with human attribution from day one: an agent that writes
        an event is an Actor, exactly like a person, and every `*_id` in the system points at the
        same spine. Idempotent - it runs on every boot, and adding an agent is one row.

        **ON CONFLICT DO NOTHING, not read-then-insert** (audit finding): two workers booting at
        the same instant both saw the name absent and both inserted, minting twins. The insert now
        leans on `uq_actors_agent_display_name`, so a concurrent boot loses its duplicate to the
        constraint instead of racing past a stale read; `RETURNING` counts only the rows that
        actually landed. `id`/`scopes`/`status` are set here because a multi-row core INSERT does
        not apply the ORM's Python-side column defaults (`created_at`'s server default still does).
        """
        if not names:
            return 0
        rows = [
            {
                "id": uuid.uuid4(),
                "kind": ActorKind.AGENT.value,
                "display_name": name,
                "scopes": [],
                "status": ActorStatus.ACTIVE.value,
            }
            for name in names
        ]
        result = await session.execute(
            pg_insert(cls)
            .values(rows)
            .on_conflict_do_nothing(index_elements=["display_name"], index_where=text("kind = 'agent'"))
            .returning(cls.id)
        )
        return len(result.all())
