# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`just evals --suite bot-selection` - do the published bots still route (ADR-045)?

Every published bot carries selection cases: a goal, and either itself or `none` (a near-miss it must
leave alone). This suite re-runs each bot's cases through the publish gate itself
(`operations/bot_selection.test_selection`: the Lead, decision-only, on the roster its owner's tasks
plan from) and reports wins and refusals per bot. The gate scored a bot against the roster AT
PUBLISH TIME; rosters drift as bots join, so this nightly re-run is the drift alarm.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

from prismoid.schemas.bots import NO_BOT, SelectionReport


@dataclass(frozen=True)
class Head:
    """One published bot, where it lives and who owns it."""

    workspace_id: uuid.UUID
    owner_actor_id: uuid.UUID
    project_id: uuid.UUID | None
    name: str
    version: int


async def _heads() -> list[Head]:
    """Every published head that ships cases, swept across every workspace (ADR-061)."""
    from sqlalchemy import func, select

    from prismoid.db.models.bot import BotRecord, BotStatus
    from prismoid.db.session import session_scope

    async with session_scope(platform=True) as session:
        published = select(BotRecord).where(BotRecord.status == BotStatus.PUBLISHED.value).subquery()
        latest = (
            select(
                published.c.project_id,
                published.c.owner_actor_id,
                published.c.name,
                func.max(published.c.version).label("v"),
            )
            .group_by(published.c.project_id, published.c.owner_actor_id, published.c.name)
            .subquery()
        )
        rows = await session.scalars(
            select(BotRecord)
            .join(
                latest,
                (BotRecord.name == latest.c.name)
                & (BotRecord.version == latest.c.v)
                & (BotRecord.project_id.is_not_distinct_from(latest.c.project_id))
                & (BotRecord.owner_actor_id == latest.c.owner_actor_id),
            )
            .where(BotRecord.status == BotStatus.PUBLISHED.value)
        )
        return [
            Head(r.workspace_id, r.owner_actor_id, r.project_id, r.name, r.version)
            for r in rows
            if (r.manifest or {}).get("cases")
        ]


async def _scored(head: Head) -> SelectionReport:
    """One bot's cases through the publish gate, as its owner, in its own workspace."""
    from prismoid.db.models.actor import Actor
    from prismoid.db.session import session_scope
    from prismoid.operations import bot_selection

    async with session_scope(head.workspace_id) as session:
        owner = await session.get(Actor, head.owner_actor_id)
        if owner is None:
            raise LookupError(f"bot {head.name}: its owner is gone")
        return await bot_selection.test_selection(
            session, owner, head.name, version=head.version, project_id=head.project_id
        )


def _report(reports: list[SelectionReport]) -> float:
    """Wins and refusals per bot, then every misroute; the share of cases that passed."""
    print("  per-bot  (wins = its own goals won · refusals = near-misses left alone)")
    for report in sorted(reports, key=lambda r: r.bot):
        own = [c for c in report.cases if c.expected != NO_BOT]
        near = [c for c in report.cases if c.expected == NO_BOT]
        wins = sum(c.ok for c in own) / len(own) if own else 1.0
        refusals = sum(c.ok for c in near) / len(near) if near else 1.0
        flag = "" if report.green else "  ←"
        print(f"    {report.bot:<24} wins {wins:5.0%}  refusals {refusals:5.0%}{flag}")

    misses = [(r.bot, c) for r in reports for c in r.cases if not c.ok]
    if misses:
        print("\n  misroutes:")
        for bot, c in misses:
            print(f"    ✗ {bot:<24} expect {c.expected:<18} got {c.chosen:<18} · {c.goal[:48]}")

    total = sum(r.total for r in reports)
    return sum(r.passed for r in reports) / total if total else 0.0


async def run(concurrency: int = 6, threshold: float = 0.95) -> int:
    heads = await _heads()
    if not heads:
        print("── bot-selection: no published bots ship cases yet - nothing to score ──")
        return 0  # an empty install is not a failure; the suite grows as bots do

    print(f"── bot-selection: {len(heads)} bot(s) on the Lead, {concurrency} at a time ──\n")
    gate = asyncio.Semaphore(concurrency)

    async def one(head: Head) -> SelectionReport:
        async with gate:
            return await _scored(head)

    accuracy = _report(list(await asyncio.gather(*(one(h) for h in heads))))
    print(f"\n  accuracy {accuracy:.0%}  (threshold {threshold:.0%})")
    return 0 if accuracy >= threshold else 1
