# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Channels - request bodies and responses (ADR-056). `*In` = request, `*Out` = response."""

from __future__ import annotations

import datetime as dt
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from prismoid.api.serializers.conversations import MessageOut, message_out
from prismoid.db.models.bot_channel import BotChannel


class ChannelCreateIn(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: str = "webhook"
    project_id: UUID | None = None
    label: str = Field(default="", max_length=120)
    # BYO BotFather token for kind=telegram: Fernet at rest, never echoed back.
    telegram_token: str | None = Field(default=None, max_length=100)


class ChannelUpdateIn(BaseModel):
    model_config = ConfigDict(frozen=True)

    input_mode: str | None = None  # data | instruction - the risk dial, named in the UI
    allowlist: list[str] | None = None
    budget_usd_per_turn: float | None = Field(default=None, ge=0.05, le=100)
    daily_message_cap: int | None = Field(default=None, ge=1, le=10_000)
    # May non-owner senders on this channel reach the door's work rungs?
    work_grant: bool | None = None


class ChannelOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    bot_name: str
    kind: str
    label: str
    status: str
    address: str | None
    input_mode: str
    allowlist: list[str]
    budget_usd_per_turn: str
    daily_message_cap: int
    work_grant: bool
    last_message_at: dt.datetime | None
    last_error: str | None
    created_at: dt.datetime


def channel_out(row: BotChannel) -> ChannelOut:
    return ChannelOut(
        id=row.id,
        bot_name=row.bot_name,
        kind=row.kind,
        label=row.label,
        status=row.status,
        address=row.address,
        input_mode=row.input_mode,
        allowlist=row.allowlist(),
        budget_usd_per_turn=str(row.budget_usd_per_turn),
        daily_message_cap=row.daily_message_cap,
        work_grant=row.work_grant,
        last_message_at=row.last_message_at,
        last_error=row.last_error,
        created_at=row.created_at,
    )


class ChannelMintOut(ChannelOut):
    """Creation and rotation answer with the raw token ONCE - the ck_ ceremony."""

    token: str | None = None


class ChannelMessageIn(BaseModel):
    model_config = ConfigDict(frozen=True)

    message: str = Field(min_length=1, max_length=60_000)
    sender: str = Field(default="", max_length=255)
    thread: str = Field(default="main", max_length=120)  # one conversation per (channel, thread)
    idempotency_key: str | None = Field(default=None, max_length=255)


# A channel's feed IS its thread conversation's lines: one message shape, every
# transport. `ChannelMessageOut` is the conversation lane's `MessageOut`, re-exported so the
# door's wire stays one file per resource.
ChannelMessageOut = MessageOut
channel_message_out = message_out


class InboundAcceptedOut(BaseModel):
    """202 for every accepted OR refused line (a refusal is a row wearing its reason): where it
    landed, the verdict the door gave, and the task if one started."""

    model_config = ConfigDict(frozen=True)

    seq: int
    status: str  # accepted | refused
    verdict: str
    conversation_id: str
    task_id: str | None
    detail: str


class EmailInboundIn(BaseModel):
    """The ONE generic inbound-parse shape - Postmark, Mailgun, and SES-to-SNS
    can all be adapted to POST this; the provider is operator infrastructure, like the
    storage endpoint. `from` is spelled with an alias: a reserved word is not a wire choice."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    to: str = Field(min_length=3, max_length=255)
    from_address: str = Field(min_length=3, max_length=255, alias="from")
    subject: str = Field(default="", max_length=500)
    text: str = Field(default="", max_length=60_000)
    headers: dict[str, str] = Field(default_factory=dict)
    message_id: str | None = Field(default=None, max_length=255)
