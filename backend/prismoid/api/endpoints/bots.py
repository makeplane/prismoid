# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Bots - user-authored agents, draft → test → publish. Publishing needs green evals.

The selection eval and the `bot_lesson` draft are model calls, so the API never runs them: the
`draft` and `test` routes only ENQUEUE onto the worker's bot queue and answer 202;
the caller polls the bot for the landed result. Every route requires an Actor; the
Project role matrix is enforced one layer down, in `operations/bots.py`, because authorization
needs the row and a view never fetches one.
"""

from __future__ import annotations

from uuid import UUID

from prismoid.api.deps import ActorDep, PageDep, SessionDep
from prismoid.api.serializers.bots import (
    BotAccessOut,
    BotArtifactOut,
    BotConnectorAccessOut,
    BotCreateIn,
    BotFaceIn,
    BotJobOut,
    BotOut,
    BotPinIn,
    BotPushIn,
    BotRunOut,
    BotStatsOut,
    bot_out,
    to_source,
)
from prismoid.api.serializers.common import PageOut
from prismoid.api.serializers.routines import RoutineOut, routine_out
from prismoid.errors import NotFound
from prismoid.operations import bots as ops
from prismoid.operations.bot_access import access_of


async def create_bot(body: BotCreateIn, actor: ActorDep, session: SessionDep) -> BotOut:
    record = await ops.push(
        session, actor=actor, name=str(body.name), source=to_source(body), project_id=body.project_id
    )
    return bot_out(record)


async def list_bot_routines(
    name: str, actor: ActorDep, session: SessionDep, project_id: UUID | None = None
) -> list[RoutineOut]:
    rows = await ops.routines_of(session, actor=actor, name=name, project_id=project_id)
    return [routine_out(r, with_upcoming=False) for r in rows]


async def bot_access(
    name: str, actor: ActorDep, session: SessionDep, project_id: UUID | None = None
) -> BotAccessOut:
    access = await access_of(session, actor=actor, name=name, project_id=project_id)
    return BotAccessOut(connectors=[BotConnectorAccessOut(**c.model_dump()) for c in access.connectors])


async def bot_stats(
    name: str, actor: ActorDep, session: SessionDep, project_id: UUID | None = None
) -> BotStatsOut:
    s = await ops.stats_of(session, actor=actor, name=name, project_id=project_id)
    return BotStatsOut(
        spend_usd_total=str(s.spend_usd_total),
        spend_usd_30d=str(s.spend_usd_30d),
        runs=s.runs,
        last_run_at=s.last_run_at,
        memory_count=s.memory_count,
        routine_count=s.routine_count,
    )


async def draft_bot(
    name: str, actor: ActorDep, session: SessionDep, project_id: UUID | None = None
) -> BotJobOut:
    key = await ops.request_draft(session, actor=actor, name=name, project_id=project_id)
    return BotJobOut(job=key)


async def test_bot(
    name: str, actor: ActorDep, session: SessionDep, project_id: UUID | None = None
) -> BotJobOut:
    key = await ops.request_test(session, actor=actor, name=name, project_id=project_id)
    return BotJobOut(job=key)


async def list_bots(
    actor: ActorDep, session: SessionDep, page: PageDep, project_id: UUID | None = None
) -> PageOut[BotOut]:
    rows = await ops.list_bots(session, actor, project_id=project_id, limit=page.limit, cursor=page.cursor)
    next_cursor = rows[-1].cursor() if len(rows) == page.limit else None
    return PageOut(items=[bot_out(r) for r in rows], next_cursor=next_cursor)


async def get_bot(name: str, actor: ActorDep, session: SessionDep, project_id: UUID | None = None) -> BotOut:
    records = await ops.versions(session, actor, name, project_id=project_id)
    if not records:
        raise NotFound(f"no bot named {name!r}")
    published = [r for r in records if r.status == "published"]
    return bot_out(published[0] if published else records[0])  # head, else newest (a draft-only bot)


async def update_draft(
    name: str,
    body: BotPushIn,
    actor: ActorDep,
    session: SessionDep,
    project_id: UUID | None = None,
) -> BotOut:
    record = await ops.push(session, actor=actor, name=name, source=to_source(body), project_id=project_id)
    return bot_out(record)


async def set_face(
    name: str,
    body: BotFaceIn,
    actor: ActorDep,
    session: SessionDep,
    project_id: UUID | None = None,
) -> BotOut:
    record = await ops.set_face(
        session,
        actor=actor,
        name=name,
        icon=body.icon,
        icon_hue=body.icon_hue,
        title=body.title,
        clear_icon=body.clear_icon,
        clear_hue=body.clear_hue,
        clear_title=body.clear_title,
        work_mode=body.work_mode,
        task_budget_usd=body.task_budget_usd,
        threads=body.threads,
        project_id=project_id,
    )
    return bot_out(record)


async def publish_bot(
    name: str, actor: ActorDep, session: SessionDep, project_id: UUID | None = None
) -> BotOut:
    record = await ops.publish(session, actor=actor, name=name, project_id=project_id)  # 409 if not green
    return bot_out(record)


async def archive_bot(
    name: str, actor: ActorDep, session: SessionDep, project_id: UUID | None = None
) -> BotOut:
    record = await ops.archive(session, actor=actor, name=name, project_id=project_id)
    return bot_out(record)


async def list_versions(
    name: str, actor: ActorDep, session: SessionDep, project_id: UUID | None = None
) -> list[BotOut]:
    records = await ops.versions(session, actor, name, project_id=project_id)
    if not records:
        raise NotFound(f"no bot named {name!r}")
    return [bot_out(r) for r in records]


async def set_pin(
    name: str, body: BotPinIn, actor: ActorDep, session: SessionDep, project_id: UUID | None = None
) -> BotOut:
    record = await ops.set_pinned(session, actor, name, pinned=body.pinned, project_id=project_id)
    return bot_out(record)


async def list_runs(name: str, actor: ActorDep, session: SessionDep) -> list[BotRunOut]:
    return [BotRunOut.model_validate(r) for r in await ops.runs_of(session, actor, name)]


async def list_bot_artifacts(name: str, actor: ActorDep, session: SessionDep) -> list[BotArtifactOut]:
    return [BotArtifactOut.model_validate(a) for a in await ops.artifacts_of(session, actor, name)]
