# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A real provider call, inside a real task, landing a real row on the ledger.

This is the metering validation instrument. It goes through the *whole* seam:

    resolve(class) → MeteredModel → emit_usage → operations.usage.record
        → usage_events row  +  project_usage_daily rollup  +  a `usage.recorded` event on the stream

Nothing is stubbed. If the ledger row is missing, Rule 8 is broken, and the whole ledger with it.
"""

from __future__ import annotations

import asyncio
import sys

from pydantic_ai import Agent

from prismoid.ai.gateway.meter import MeterContext
from prismoid.ai.gateway.resolve import resolve
from prismoid.db.models.task import Task
from prismoid.db.session import session_scope
from prismoid.operations import models as model_ops
from prismoid.operations import usage as usage_ops
from prismoid.schemas.ids import TaskId
from prismoid.schemas.model_class import ModelClass


async def main(task_id: str, prompt: str) -> None:
    async with session_scope() as session:
        task = await session.get(Task, __import__("uuid").UUID(task_id))
        assert task is not None, f"no task {task_id}"

        catalog = await model_ops.catalog(session, enabled_only=True)
        model = resolve(
            ModelClass.FAST_EXECUTOR,
            catalog,
            emit_usage=usage_ops.emitter(session),  # ← the seam: a closure, not a session
            context=MeterContext(task_id=TaskId(task.id), project_id=None),
        )

        result = await Agent(model).run(prompt)
        await session.commit()

    print(f"  answer: {str(result.output).strip()[:80]!r}")


if __name__ == "__main__":
    asyncio.run(
        main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "What is 7 times 6? Just the number.")
    )
