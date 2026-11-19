# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Feature flags - a Project override on top of a global default.

Three places a flag's value can come from, and the precedence is the whole file:

1. a **row for this project** - the override, and the reason a flag exists at all;
2. a **row with a null project** - the global default, set by an operator;
3. `PRISMOID_FEATURE_DEFAULTS` - the *shipped* default, so a fresh database is not a database with
   every flag silently off.

(3) matters more than it looks. A flag that defaults to off because nobody inserted a row is a
feature that is off in production and on in every developer's head.
"""

from __future__ import annotations

import uuid
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.conf import settings
from prismoid.db.models.feature import Feature

PARALLEL_FANOUT: Final = "prismoid:parallel-fanout"
FLEET: Final = "prismoid:fleet"


async def enabled(session: AsyncSession, name: str, project_id: uuid.UUID | None = None) -> bool:
    rows = await Feature.rows_for(session, name, project_id)

    for row in rows:  # the project's own row wins outright - that is what an override is
        if row.project_id is not None:
            return row.enabled

    for row in rows:  # otherwise the global row, if an operator has set one
        return row.enabled

    return settings.feature_defaults.get(name, False)  # otherwise what we shipped
