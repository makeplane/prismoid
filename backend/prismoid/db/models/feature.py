# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Feature - a flag scoped to a Project or global.

`prismoid:parallel-fanout`, `prismoid:fleet`. A null `project_id` is the
global default; a row with a project overrides it there. Defaults also arrive from
`PRISMOID_FEATURE_DEFAULTS`, so a fresh database is not a database with every flag off by
accident.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from prismoid.db.base import Base, uuid_pk


class Feature(Base):
    __tablename__ = "features"
    __table_args__ = (
        UniqueConstraint("name", "project_id", name="features_key"),
        # The UniqueConstraint above treats two (name, NULL) rows as distinct - SQL NULLs never
        # compare equal - so two GLOBAL rows for one flag could coexist and `rows_for` would pick
        # arbitrarily. A partial unique index over the global rows is the only thing that forbids
        # it (audit finding); a per-project override still lives under the constraint.
        Index("uq_features_name_global", "name", unique=True, postgresql_where=text("project_id IS NULL")),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(80), nullable=False)  # e.g. "prismoid:fleet"
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        default=None,  # NULL ⇒ the global default
    )
    enabled: Mapped[bool] = mapped_column(nullable=False, default=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    @classmethod
    async def rows_for(cls, session: AsyncSession, name: str, project_id: uuid.UUID | None) -> list[Feature]:
        """This flag's rows: the project's override and the global default, if either exists."""
        scope = cls.project_id.in_([project_id, None]) if project_id else cls.project_id.is_(None)
        return list(await session.scalars(select(cls).where(cls.name == name, scope)))
