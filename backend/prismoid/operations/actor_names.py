# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""An actor's name as another member reads it - their own words, so made safe (ADR-062).

A leaf: it reads actor rows and nothing else, so any verb that names a person asks it instead of
importing the verb module that happened to hold the rule.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.context.thread import safe_name
from prismoid.db.models.actor import Actor


async def name_of(session: AsyncSession, actor_id: uuid.UUID | None) -> str:
    """One person's safe name; an unknown or missing actor reads as the empty name, made safe."""
    who = await session.get(Actor, actor_id) if actor_id is not None else None
    return safe_name(who.display_name if who is not None else "")


async def names_of(session: AsyncSession, ids: Iterable[uuid.UUID | None]) -> dict[uuid.UUID | None, str]:
    """`name_of` for several people in one query: a mapping that answers every id it is asked,
    None and an unknown one included, the way `name_of` would."""
    names = await Actor.names(session, {a for a in ids if a is not None})
    return defaultdict(lambda: safe_name(""), {a: safe_name(n) for a, n in names.items()})
