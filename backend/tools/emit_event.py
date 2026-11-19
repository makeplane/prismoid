# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Append a durable event through the real code path — Task.next_seq → TaskEvent.append → pg_notify.

Used to prove live fan-out from a *second process*. It calls exactly what
the job will call: no direct SQL, no shortcut around the append path.
"""

from __future__ import annotations

import asyncio
import sys
from uuid import UUID

from prismoid.db.base import utcnow
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.task import Task
from prismoid.db.session import session_scope
from prismoid.schemas.events import TaskStatusChanged
from prismoid.schemas.ids import TaskId
from prismoid.schemas.plan import TaskStatus


async def main(task_id: str, status: str) -> None:
    async with session_scope() as session:
        task = await session.get(Task, UUID(task_id))
        assert task is not None
        seq = await task.next_seq(session)
        await TaskEvent.append(
            session,
            TaskStatusChanged(
                seq=seq,
                task_id=TaskId(task.id),
                at=utcnow(),
                status=TaskStatus(status),
                reason="emitted by the validation, from a second process",
            ),
        )
        await session.commit()
        print(f"  committed seq={seq} type=task.status status={status}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))
