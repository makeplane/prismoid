# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Who may touch a bot, and which version of it an edit means: the rules every bot verb asks first.

A leaf: it imports rows and errors only, so `bots`, `bot_selection`, `bot_lessons` and
`bot_access` all ask it without importing each other. Nothing here commits.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models.actor import Actor
from prismoid.db.models.bot import BotRecord
from prismoid.db.models.project import Project, Role
from prismoid.errors import NotFound, ValidationFailed


async def authorize(
    session: AsyncSession, actor: Actor, project_id: uuid.UUID | None, *, min_role: Role
) -> None:
    """The project role matrix, applied to bots. A personal bot (`project_id is None`) needs no check - the
    scope predicate already restricts every query to this actor's own rows, so there is nothing
    a role could add. A project bot goes through `Project.get_for`, the ONE fetch path, which 404s a
    stranger and 403s a member who lacks the role."""
    if project_id is not None:
        await Project.get_for(session, project_id, actor, min_role=min_role)


def working_version(draft: BotRecord | None, head: BotRecord | None) -> BotRecord | None:
    """The version an edit or a lesson revises: the author's draft ONLY when it is ahead of the
    published head; otherwise the head itself.

    A live teach found the miss this encodes: an ABANDONED draft (v4, red, an over-claiming
    description the author had already superseded by publishing v5) outranked the head because
    "latest draft or head" reads the workbench before the shelf - and the lesson landed on the
    bad description. A draft older than the head is not work in progress; it is history that
    lost. Same rule the web's detail page renders as "draft vN in progress".
    """
    if draft is not None and (head is None or draft.version > head.version):
        return draft
    return head


async def working_record(
    session: AsyncSession, actor: Actor, name: str, *, project_id: uuid.UUID | None
) -> BotRecord:
    """The caller's draft or the published head (`working_version`), or a 404 naming the bot.
    Reads the draft first, then the head; takes no lock."""
    draft = await BotRecord.latest_draft(session, name, actor.id, project_id=project_id)
    head = await BotRecord.head(session, name, project_id=project_id, owner_actor_id=actor.id)
    record = working_version(draft, head)
    if record is None:
        raise NotFound(f"no bot named {name!r} in this scope")
    return record


def require_scorable(name: str, manifest: Mapping[str, object]) -> None:
    """A newborn cannot be scored, and a 0/0 report would just wear a color - name the gap
    instead, in the order an author will fix it (completeness is publish's ask). The CLI's
    in-process test and the web's enqueue ask the same question, so they teach identically."""
    if not str(manifest.get("description", "")).strip():
        raise ValidationFailed(
            f"{name} has no routing description yet - answer the three questions on Configure "
            "(what it does, when it steps in, what it refuses) before testing."
        )
    if not manifest.get("cases"):
        raise ValidationFailed(
            f"{name} has no selection cases yet - add at least one goal it should win before testing."
        )
