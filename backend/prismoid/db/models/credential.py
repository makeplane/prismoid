# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Credential - a User's argon2id password hash. One row per user, replaced on change.

Separate from `users` on purpose: a password hash has a different read profile (only at login),
a different sensitivity, and a different lifecycle (rotation) from a profile row. Nothing that
selects a user for display should be able to accidentally carry their hash into a response.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from prismoid.db.base import Base, created_at


class Credential(Base):
    __tablename__ = "credentials"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.actor_id", ondelete="CASCADE"), primary_key=True
    )
    # argon2id, encoded with its parameters - so a parameter change rehashes on next login
    # instead of locking everyone out.
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[dt.datetime] = created_at()
    updated_at: Mapped[dt.datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )
