# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""AdminAction - the operator's own flight recorder. One row per admin mutation, append-only.

`task_events` is the wrong home for these on purpose: it is per-task, PK'd `(task_id, seq)`, and
wire-identical to the stream - "suspended user X" and "installed price book v4" have no task and
no stream. So admin actions get their own append-only ledger, same discipline as `usage_events`
(Rule 10): written **in the same transaction** as the mutation they record, never updated, never
deleted. An admin surface without an audit trail is a root shell with extra steps.

The row answers three questions, always: WHO (`admin_actor_id`), DID WHAT (`action`, a fixed
dot-vocabulary: `user.suspend`, `task.cancel`, `model.patch`, `prices.put`, …), TO WHAT
(`subject_type` + `subject_id` - a string, not a UUID, because a catalog subject is a
`provider:model_id` ref). `detail` carries the small facts worth keeping (the reason given, the
fields changed) - keys and summaries, never secrets and never request bodies wholesale.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import ForeignKey, Index, String, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from prismoid.db.base import Base, created_at, keyset_before, uuid_pk


class AdminAction(Base):
    __tablename__ = "admin_actions"
    __table_args__ = (
        # The audit list reads newest-first by keyset - same (created_at, id) recipe as tasks.
        Index("ix_admin_actions_created_at_id", "created_at", "id"),
        # "Everything ever done to this user/model/task" - the subject's own history.
        Index("ix_admin_actions_subject", "subject_type", "subject_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()

    # Plain FK, not SET NULL: actors are never deleted (attribution outlives the profile - the
    # GDPR path soft-deletes the User and keeps the Actor precisely so ledgers like this stay whole).
    admin_actor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("actors.id"), nullable=False)

    action: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(32), nullable=False)  # user | task | model | …
    subject_id: Mapped[str] = mapped_column(String(120), nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)

    created_at: Mapped[dt.datetime] = created_at()

    @classmethod
    async def page(
        cls,
        session: AsyncSession,
        *,
        subject_type: str | None = None,
        subject_id: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> list[AdminAction]:
        """Keyset pagination on `(created_at, id)`, newest first - never OFFSET.
        The subject pair narrows to one thing's history ("everything ever done to this user"),
        riding `ix_admin_actions_subject` - the index existed from day one for exactly this."""
        query = select(cls).order_by(cls.created_at.desc(), cls.id.desc()).limit(limit)
        if subject_type:
            query = query.where(cls.subject_type == subject_type)
        if subject_id:
            query = query.where(cls.subject_id == subject_id)
        if cursor is not None:
            query = query.where(keyset_before(cls, cursor))
        return list(await session.scalars(query))
