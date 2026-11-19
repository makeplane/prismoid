# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The express rung's prompt: the conversation, the turn's ask, who asks, and the ambient facts the
Lead reads. `express_ambient` reads the turn's facts; the rest is pure. `express_rung.py` runs the
rung around them."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING

from prismoid.ai import config
from prismoid.ai.context.blocks import MACHINE_REACH, reply_memory_section, where_facts
from prismoid.ai.context.thread import author_line, render_thread_md
from prismoid.db.base import utcnow
from prismoid.db.models.connector import Connector
from prismoid.operations import context as context_ops
from prismoid.operations import thread as thread_ops
from prismoid.schemas.connectors import ConnectorStatus, ServiceReach
from prismoid.schemas.thread import ThreadTranscript

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from prismoid.db.models.task import Task


def ambient_facts(
    timezone: str | None,
    now: dt.datetime,
    *,
    services: Sequence[str] | None = None,
    reach: Sequence[ServiceReach] = (),
    computer: bool = False,
) -> str:
    """The ambient facts as values: the turn author's clock, the services this turn can reach and what a
    planned step can do there, and whether computer use is on (so the Lead plans what it can and says what
    to connect)."""
    tz = timezone or "UTC"
    facts = f"# Ambient facts\n\n- current UTC time: {now:%Y-%m-%d %H:%M} UTC\n- the user's timezone: {tz}\n"
    facts += where_facts(timezone)
    if services is not None:
        facts += f"- services connected to this task: {', '.join(services) or 'none'}\n"
    for service in reach:
        if service.steps_can:
            facts += (
                f"- {service.name}: a planned step can also call {', '.join(service.steps_can)} "
                "(a call that changes something may ask the person first)\n"
            )
        if service.turned_off:
            facts += f"- {service.name}: turned off in the person's connector settings: {', '.join(service.turned_off)}\n"
    if computer:
        facts += (
            "- this task has a computer: a real browser that opens public sites, asks the person for "
            "details it lacks, and hands them the screen to sign in or pay\n"
            f"{MACHINE_REACH}\n"
        )
    return facts


async def express_ambient(session: AsyncSession, task: Task, *, reach: Sequence[ServiceReach] = ()) -> str:
    """The Lead's per-turn tail: what this turn recalled (fenced), then the ambient facts; `reach` is
    what its services do beyond the reads it mounted."""
    timezone = await thread_ops.author_timezone(session, task)
    ids = [uuid.UUID(str(i)) for i in task.connector_ids or ()]
    rows = await Connector.for_ids(session, ids, owner_id=task.creator_actor_id)
    services = [row.name for row in rows if row.status == ConnectorStatus.LIVE]
    facts = ambient_facts(
        timezone, utcnow(), services=services, reach=reach, computer=config.current().computer_use.enabled
    )
    memory = reply_memory_section(await context_ops.readable_pack(session, task))
    return f"{memory}\n{facts}" if memory else facts


def express_prompt(base: str, transcript: ThreadTranscript, *, ambient: str) -> str:
    """The Lead's prompt, stable first so a follow-up reads it from cache: the conversation, this turn's
    ask, who is asking, then the tail."""
    parts = [] if transcript.is_empty() else [render_thread_md(transcript).rstrip()]
    parts.append(base)
    if (asker := author_line(transcript)) is not None:
        # The Lead is told who is asking now, under its own heading and apart from the thread - a
        # display name is data, sanitized and quoted in the line.
        parts.append(f"# Who is asking\n\n{asker}")
    return "\n\n".join([*parts, ambient])
