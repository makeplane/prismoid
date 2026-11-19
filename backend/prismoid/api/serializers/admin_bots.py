# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Admin - the bot population's shapes (admin surface). `*Out` = response, read-only v1.

One row per bot HEAD (the newest version per owner/project/name, any status). The detail carries
the full instructions - an author's content, which is why the detail READ is audited
(`bot.view`, admin_ops law 2) exactly like the task peek. Moderation verbs are deliberately
absent until a real need names them (the capability law, server-side).
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class AdminBotAuthorOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    email: str
    name: str


class AdminBotOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID  # the head row's registry id - the detail route's address
    name: str
    version: int
    status: str  # draft | published | archived
    origin: str
    author: AdminBotAuthorOut | None
    project_id: UUID | None  # None = personal
    eval_status: str
    pinned: bool
    description: str
    created_at: dt.datetime


class AdminBotVersionOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    version: int
    status: str
    eval_status: str
    created_at: dt.datetime


class AdminBotDetailOut(AdminBotOut):
    instructions: str
    versions: list[AdminBotVersionOut]


def _description(bot: Any) -> str:
    return str((bot.manifest or {}).get("description") or "")


def admin_bot_out(row: dict[str, Any]) -> AdminBotOut:
    bot, author = row["bot"], row["author"]
    return AdminBotOut(
        id=bot.id,
        name=bot.name,
        version=bot.version,
        status=bot.status,
        origin=bot.origin,
        author=None
        if author is None
        else AdminBotAuthorOut(id=author.actor_id, email=author.email, name=author.name),
        project_id=bot.project_id,
        eval_status=bot.eval_status,
        pinned=bot.pinned,
        description=_description(bot),
        created_at=bot.created_at,
    )


def admin_bot_detail_out(row: dict[str, Any]) -> AdminBotDetailOut:
    base = admin_bot_out(row)
    bot = row["bot"]
    return AdminBotDetailOut(
        **base.model_dump(),
        instructions=bot.instructions,
        versions=[
            AdminBotVersionOut(
                id=v.id,
                version=v.version,
                status=v.status,
                eval_status=v.eval_status,
                created_at=v.created_at,
            )
            for v in row["versions"]
        ],
    )
