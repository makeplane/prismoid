# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""ModelCatalogEntry - one available model: provider, id, family, classes, context, base_url, enabled.

**This table is why adding a model is data, not a deploy**. `resolve()` builds its chains
from the *enabled rows here*, per model class - so a new model is one INSERT and it is
routed on the next request. There is no code path that names a model; Rule 7 keeps model strings
inside `ai/gateway/providers.py`, and even there they are protocol constructors, not a registry.

Added **disabled** by default: `--evaluate` scores it, `--enable` is a second, deliberate act.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import Index, String, UniqueConstraint, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from prismoid.db.base import Base, created_at, uuid_pk


class ModelCatalogEntry(Base):
    __tablename__ = "model_catalog"
    __table_args__ = (
        UniqueConstraint("provider", "model_id", name="model_catalog_key"),
        Index("ix_model_catalog_enabled", "enabled"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    model_id: Mapped[str] = mapped_column(String(120), nullable=False)
    family: Mapped[str] = mapped_column(String(40), nullable=False)
    protocol: Mapped[str] = mapped_column(String(32), nullable=False)  # schemas.model_class.Protocol

    # The model classes this model can serve. The ONLY vocabulary application code speaks.
    classes: Mapped[list[str]] = mapped_column(nullable=False, default=list)

    context_window: Mapped[int] = mapped_column(nullable=False, default=0)
    base_url: Mapped[str | None] = mapped_column(String(500), default=None)  # T2: OpenAI-compatible
    enabled: Mapped[bool] = mapped_column(nullable=False, default=False)

    eval_scores: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)  # --evaluate

    created_at: Mapped[dt.datetime] = created_at()
    updated_at: Mapped[dt.datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    @classmethod
    async def all(cls, session: AsyncSession, *, enabled_only: bool = True) -> list[ModelCatalogEntry]:
        query = select(cls).order_by(cls.provider, cls.model_id)
        if enabled_only:
            query = query.where(cls.enabled.is_(True))
        return list(await session.scalars(query))

    @classmethod
    async def get(cls, session: AsyncSession, provider: str, model_id: str) -> ModelCatalogEntry | None:
        return await session.scalar(select(cls).where(cls.provider == provider, cls.model_id == model_id))
