# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Channels - a bot's external doors (ADR-056).

The `/v1/channels/{channel_id}/messages` pair is AUTH EXCEPTION #6 (the rt_ fire door's
family): its Bearer is the channel's own single-purpose `bc_` token, never an Actor
credential - it can start and read THAT channel's turns and nothing else. Everything else
here is owner-authenticated CRUD.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import Request

from prismoid.api.deps import ActorDep, SessionDep
from prismoid.api.serializers.channels import (
    ChannelCreateIn,
    ChannelMessageIn,
    ChannelMessageOut,
    ChannelMintOut,
    ChannelOut,
    ChannelUpdateIn,
    EmailInboundIn,
    InboundAcceptedOut,
    channel_message_out,
    channel_out,
)
from prismoid.errors import Unauthorized
from prismoid.operations import channels as ops
from prismoid.schemas.channels import ChannelStatus
from prismoid.schemas.ids import CHANNEL_TOKEN_PREFIX


def _bearer_token(request: Request) -> str:
    raw = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
    if not raw.startswith(CHANNEL_TOKEN_PREFIX):
        raise Unauthorized("this door takes the channel's own bc_ token")
    return raw


async def create_bot_channel(
    name: str, body: ChannelCreateIn, actor: ActorDep, session: SessionDep
) -> ChannelMintOut:
    row, raw = await ops.create_channel(
        session,
        actor=actor,
        bot_name=name,
        kind=body.kind,
        project_id=body.project_id,
        label=body.label,
        telegram_token=body.telegram_token,
    )
    return ChannelMintOut(**channel_out(row).model_dump(), token=raw)


async def list_bot_channels(name: str, actor: ActorDep, session: SessionDep) -> list[ChannelOut]:
    return [channel_out(r) for r in await ops.list_channels(session, actor=actor, bot_name=name)]


async def update_channel(
    channel_id: UUID, body: ChannelUpdateIn, actor: ActorDep, session: SessionDep
) -> ChannelOut:
    row = await ops.update_channel(
        session,
        actor=actor,
        channel_id=channel_id,
        input_mode=body.input_mode,
        allowlist=body.allowlist,
        budget_usd_per_turn=body.budget_usd_per_turn,
        daily_message_cap=body.daily_message_cap,
        work_grant=body.work_grant,
    )
    return channel_out(row)


async def remove_channel(channel_id: UUID, actor: ActorDep, session: SessionDep) -> dict[str, str]:
    await ops.remove_channel(session, actor=actor, channel_id=channel_id)
    return {"status": "removed"}


async def rotate_channel_token(channel_id: UUID, actor: ActorDep, session: SessionDep) -> ChannelMintOut:
    row, raw = await ops.rotate_token(session, actor=actor, channel_id=channel_id)
    return ChannelMintOut(**channel_out(row).model_dump(), token=raw)


async def disable_channel(channel_id: UUID, actor: ActorDep, session: SessionDep) -> ChannelOut:
    return channel_out(
        await ops.set_status(session, actor=actor, channel_id=channel_id, status=ChannelStatus.DISABLED)
    )


async def enable_channel(channel_id: UUID, actor: ActorDep, session: SessionDep) -> ChannelOut:
    return channel_out(
        await ops.set_status(session, actor=actor, channel_id=channel_id, status=ChannelStatus.ACTIVE)
    )


async def channel_log(
    channel_id: UUID, actor: ActorDep, session: SessionDep, thread: str = "main", since: int = 0
) -> list[ChannelMessageOut]:
    row = await ops.get_channel(session, actor=actor, channel_id=channel_id)
    return [channel_message_out(m) for m in await ops.feed(session, row, thread_key=thread, since=since)]


# ── the door: bc_-authenticated, no Actor (exception #6) ─────────────────────────────────────


async def post_channel_message(
    channel_id: UUID, body: ChannelMessageIn, request: Request, session: SessionDep
) -> InboundAcceptedOut:
    channel = await ops.authenticate(session, channel_id, _bearer_token(request))
    result = await ops.accept_inbound(
        session,
        channel,
        sender=body.sender or "api",
        body=body.message,
        thread_key=body.thread,
        provider_message_id=body.idempotency_key,
    )
    return InboundAcceptedOut(**result.model_dump())


async def get_channel_messages(
    channel_id: UUID, request: Request, session: SessionDep, thread: str = "main", since: int = 0
) -> list[ChannelMessageOut]:
    channel = await ops.authenticate(session, channel_id, _bearer_token(request), read=True)
    return [channel_message_out(m) for m in await ops.feed(session, channel, thread_key=thread, since=since)]


async def inbound_email(body: EmailInboundIn, request: Request, session: SessionDep) -> InboundAcceptedOut:
    """Auth exception #7: the provider inbound webhook - one shared header secret, then the
    address resolves the channel. The sender allowlist, the caps, and the fence all apply in
    `accept_inbound`; an unknown sender is a refused LEDGER ROW, no bounce (backscatter)."""
    secret = request.headers.get("X-Prismoid-Channel-Secret", "")
    channel = await ops.authenticate_email_inbound(session, secret=secret, to=body.to)
    text = f"{body.subject}\n\n{body.text}".strip() if body.subject else body.text
    result = await ops.accept_inbound(
        session,
        channel,
        sender=body.from_address,
        body=text,
        thread_key=ops.email_thread_key(body.headers, body.subject, body.message_id),
        provider_message_id=body.message_id,
        subject=body.subject,
    )
    return InboundAcceptedOut(**result.model_dump())
