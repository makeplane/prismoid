# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Routines - standing work: a saved composer payload plus a When; every fire mints an
ordinary Task. Every route requires an Actor; the role matrix is
enforced one layer down in `operations/routines.py`, because authorization needs the row and a
view never fetches one. `fire` answers 202 whatever was decided - a refused fire is a result
wearing its journaled reason, never an error; its body (`{"data": ...}`, free-form JSON that
rides UNTRUSTED into /task/inputs/trigger.json) is read streamed under the cap by
`deps.fire_payload`, so the anonymous door never buffers an unbounded POST.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import Header

from prismoid.api.deps import ActorDep, FireAuthDep, FirePayloadDep, PageDep, SessionDep
from prismoid.api.serializers.common import OkOut, PageOut
from prismoid.api.serializers.routines import (
    FireOut,
    FireTokenOut,
    PauseIn,
    RoutineCreateIn,
    RoutineOut,
    RoutineRunOut,
    RoutineUpdateIn,
    fire_out,
    routine_out,
    run_out,
)
from prismoid.operations import routines as ops


async def create_routine(body: RoutineCreateIn, actor: ActorDep, session: SessionDep) -> RoutineOut:
    return routine_out(await ops.create_routine(session, actor, **body.ops_kwargs()))


async def list_routines(
    actor: ActorDep, session: SessionDep, page: PageDep, project_id: UUID | None = None
) -> PageOut[RoutineOut]:
    rows = await ops.list_routines(
        session, actor, project_id=project_id, limit=page.limit, cursor=page.cursor
    )
    next_cursor = rows[-1].cursor() if len(rows) == page.limit else None
    # with_upcoming=False: list rows render next_fire_at only (the spend_mtd exemption's cousin)
    return PageOut(items=[routine_out(r, with_upcoming=False) for r in rows], next_cursor=next_cursor)


async def get_routine(routine_id: UUID, actor: ActorDep, session: SessionDep) -> RoutineOut:
    row = await ops.get_routine(session, actor, routine_id)
    return routine_out(row, spend_mtd=await ops.spend_month_to_date(session, row))


async def update_routine(
    routine_id: UUID, body: RoutineUpdateIn, actor: ActorDep, session: SessionDep
) -> RoutineOut:
    return routine_out(await ops.update_routine(session, actor, routine_id, **body.ops_kwargs()))


async def pause_routine(routine_id: UUID, body: PauseIn, actor: ActorDep, session: SessionDep) -> RoutineOut:
    return routine_out(await ops.pause_routine(session, actor, routine_id, note=body.note))


async def resume_routine(routine_id: UUID, actor: ActorDep, session: SessionDep) -> RoutineOut:
    return routine_out(await ops.resume_routine(session, actor, routine_id))


async def retire_routine(routine_id: UUID, actor: ActorDep, session: SessionDep) -> OkOut:
    await ops.retire_routine(session, actor, routine_id)
    return OkOut()


async def fire_routine(
    routine_id: UUID,
    auth: FireAuthDep,
    payload: FirePayloadDep,
    session: SessionDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> FireOut:
    if auth.fire_token is not None:
        result = await ops.fire_via_token(
            session, routine_id, auth.fire_token, payload=payload, idempotency_key=idempotency_key
        )
    else:
        assert auth.actor is not None  # fire_auth guarantees exactly one of the two
        result = await ops.fire(
            session, auth.actor, routine_id, payload=payload, idempotency_key=idempotency_key
        )
    return fire_out(result)


async def mint_fire_token(routine_id: UUID, actor: ActorDep, session: SessionDep) -> FireTokenOut:
    return FireTokenOut(token=await ops.mint_fire_token(session, actor, routine_id))


async def revoke_fire_token(routine_id: UUID, actor: ActorDep, session: SessionDep) -> OkOut:
    await ops.revoke_fire_token(session, actor, routine_id)
    return OkOut()


async def list_runs(
    routine_id: UUID, actor: ActorDep, session: SessionDep, page: PageDep
) -> PageOut[RoutineRunOut]:
    pairs = await ops.runs_for(session, actor, routine_id, limit=page.limit, cursor=page.cursor)
    next_cursor = pairs[-1][0].cursor() if len(pairs) == page.limit else None
    return PageOut(items=[run_out(r, status) for r, status in pairs], next_cursor=next_cursor)
