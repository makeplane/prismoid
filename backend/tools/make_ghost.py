# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Manufacture the one ghost the sweeper exists to kill.

A task that COMMITTED but was never ENQUEUED — the exact state left behind by a crash in the
window between `session.commit()` and `dispatcher.enqueue()`. The API returned 202, the row is
real, and no job will ever pick it up. It is the one failure a user cannot see and cannot retry.

We simulate it honestly: create the task through the real operation with **no dispatcher wired**
(the NullDispatcher records the intent and does nothing), then backdate it past the sweeper's
60-second staleness window.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import sys

from sqlalchemy import text

from prismoid.db.base import utcnow
from prismoid.db.models.actor import Actor
from prismoid.db.models.user import User
from prismoid.db.session import session_scope
from prismoid.operations import tasks as ops


async def main(email: str) -> None:
    async with session_scope() as session:
        user = await User.by_email(session, email)
        assert user is not None, f"no user {email}"
        actor = await session.get(Actor, user.actor_id)
        assert actor is not None

        # No dispatcher is wired in this process — NullDispatcher swallows the enqueue. That IS
        # the crash we are simulating.
        task = await ops.create_task(session, actor, goal="a ghost: committed, never enqueued")

        old = utcnow() - dt.timedelta(minutes=2)
        await session.execute(
            text("update tasks set created_at = :old where id = :id"),
            {"old": old, "id": task.id},
        )
        await session.commit()
        print(f"ghost task {task.id} — status=created, created_at={old.isoformat()}, never enqueued")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
