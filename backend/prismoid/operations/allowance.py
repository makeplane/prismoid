# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A workspace's spend allowance (ADR-103): the one seam where a distribution caps what a workspace
may spend. The open-source edition sets no allowance: `left` answers None, every door opens, nothing
parks and nothing wakes.

The doors of new spend (a task, a turn, a steer that launches, a bot run, a chat reply, a routine
fire) call `require`; the Bursar's floor, the usage brake and the gates read `left` and `exhausted`;
the gate sweep calls `wake_parks`; a proved address is told to `address_proved`. A distribution that
meters workspaces answers these from its own ledger, and every caller already does the right thing
with the answer: a door refuses with
`AllowanceSpent`, a running step parks on the "allowance spent" gate, and the sweep resumes the parks
once the allowance is back.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models.actor import Actor
from prismoid.db.models.user import User
from prismoid.errors import AllowanceSpent

# The park gate's words, where a running step meets an exhausted allowance: only the allowance
# coming back resumes it, so a person may only cancel.
SPENT_TITLE = "Spending allowance reached"
SPENT_BODY = (
    "This workspace has reached its spending allowance. The task picks up where it stopped once the "
    "allowance is back, or you can cancel it."
)
STILL_SPENT = "This workspace has still reached its spending allowance."


async def left(session: AsyncSession, workspace_id: uuid.UUID) -> Decimal | None:
    """What the workspace may still spend, in the allowance's own unit, or None where no allowance
    applies (the open-source edition: always None). Reads only."""
    del session, workspace_id
    return None


async def exhausted(session: AsyncSession, workspace_id: uuid.UUID) -> bool:
    """An allowance applies and none of it is left. Reads only."""
    remaining = await left(session, workspace_id)
    return remaining is not None and remaining <= 0


async def require(session: AsyncSession, workspace_id: uuid.UUID, *, actor: Actor | None = None) -> None:
    """The door of new spend: raises `AllowanceSpent` when an allowance applies and none is left.
    Reads only - commits nothing, locks nothing - so a door calls it before it writes anything.
    `actor` is who knocked, where the door knows, so a distribution can word the refusal for them."""
    del actor
    if await exhausted(session, workspace_id):
        raise AllowanceSpent


async def wake_parks(session: AsyncSession) -> list[uuid.UUID]:
    """The gate sweep's arm: accept every "allowance spent" park whose workspace may spend again, and
    answer the gates it accepted. Nothing parks without an allowance, so nothing wakes."""
    del session
    return []


async def address_proved(session: AsyncSession, user: User) -> None:
    """A person proved their address (the emailed code, an invite to it, or `prismoid up`'s first
    run), inside the transaction that records it: where a distribution grants an allowance for that,
    it writes the grant here, in the same commit. The open-source edition grants nothing."""
    del session, user
