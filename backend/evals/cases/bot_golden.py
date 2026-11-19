# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The per-bot golden lane (ADR-045) - end-to-end cases a bot SHIPS WITH.

`just evals --suite bot-golden` collects every published bot's `golden:` cases (stored in the
manifest, exactly where a skill's live) and runs them through the SAME harness as the main
golden suite: real tasks, real verification, `must_contain` against the delivered bytes. Each
case pins its bot onto the task's roster, so what is measured is the BOT'S WORK - whether One
would have routed to it is the selection suite's question, deliberately kept separate.

**Advisory, on the nightly cadence - never the 409 publish gate**: the gate is the sub-minute
selection eval, and ADR-015 records why a real-money end-to-end run must not replace it.
"""

from __future__ import annotations

from evals.cases.golden import Golden


async def load_cases() -> list[Golden]:
    """Published bots' golden cases → `Golden` values the run harness already speaks."""
    from sqlalchemy import func, select

    from prismoid.db.models.bot import BotRecord, BotStatus
    from prismoid.db.session import session_scope

    cases: list[Golden] = []
    async with session_scope(platform=True) as session:  # a sweep across every workspace (ADR-061)
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
        rows = list(
            await session.scalars(
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
        )
        for record in rows:
            manifest = record.manifest or {}
            for i, raw in enumerate(manifest.get("golden", []), start=1):
                if not isinstance(raw, dict) or not raw.get("goal"):
                    continue
                cases.append(
                    Golden(
                        name=f"{record.name}-golden-{i}",
                        goal=str(raw["goal"]),
                        inputs=tuple(str(p) for p in raw.get("inputs", [])),
                        must_contain=tuple(str(m) for m in raw.get("must_contain", [])),
                        bots=(record.name,),
                    )
                )
    return cases
