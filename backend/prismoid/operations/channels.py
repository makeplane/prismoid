# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Channels - a bot's external doors (ADR-056), re-plumbed onto Conversations.

The whole constitution in one file's shape:

* every inbound message lands as a USER LINE in the channel's thread conversation (one
  conversation per `(channel, thread_key)` - an email thread, a Telegram chat, a webhook
  caller's thread) and goes through the SAME door as a web message (`chat.receive`): the
  bot answers conversationally; work mints an ordinary Task only when the ladder says so,
  and an outsider never starts spend (rung 2 - `work_grant` is the owner's dial);
* refusals (allowlist, caps, disabled, kill switch) are ROWS wearing reasons - never silence;
* the reply path is `transport_reply`: the email kind replies AS the bot, Telegram sends to
  the chat, the webhook kind's transport is the feed itself (the caller polls);
* approve/budget/connect gates never leave the product: nothing here can resolve one.

The channel ledger (`channel_messages`) is REMOVED, replaced by `messages`: two
message tables for one thread was the DM-as-task mistake in a new coat.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import re
import uuid
from email.utils import parseaddr
from typing import Any, NamedTuple

import logfire
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.conf import settings
from prismoid.db.base import utcnow
from prismoid.db.models import tenant
from prismoid.db.models.actor import Actor
from prismoid.db.models.bot import BotRecord
from prismoid.db.models.bot_channel import BotChannel
from prismoid.db.models.conversation import Conversation
from prismoid.db.models.message import Message
from prismoid.db.models.project import Project, Role
from prismoid.db.models.workspace import WorkspaceMember
from prismoid.errors import Conflict, FeatureDisabled, NotFound, Unauthorized, ValidationFailed
from prismoid.operations import allowance
from prismoid.operations.throttle import FixedWindowLimiter
from prismoid.schemas.channels import (
    DEFAULT_BURST_PER_MINUTE,
    MAX_CHANNELS_PER_BOT,
    ChannelKind,
    ChannelStatus,
    InboundResult,
)
from prismoid.schemas.conversation import ConversationSource, MessageRole, Verdict
from prismoid.schemas.ids import hash_token, new_channel_token

_inbound_limiter = FixedWindowLimiter("channel messages")
_read_limiter = FixedWindowLimiter("channel polls")
_READ_BURST_PER_MINUTE = 60  # polling is cheap and reads no money - a looser lane than sends


def _kinds_enabled() -> set[str]:
    return {k.strip() for k in settings.channels.kinds.split(",") if k.strip()}


def _require_enabled(kind: str) -> None:
    if not settings.channels.enabled:
        raise FeatureDisabled("channels are disabled on this instance")
    if kind not in _kinds_enabled():
        raise FeatureDisabled(f"the {kind} channel kind is not enabled on this instance")


# ── the owner's verbs ─────────────────────────────────────────────────────────────────────────


async def create_channel(
    session: AsyncSession,
    *,
    actor: Actor,
    bot_name: str,
    kind: str,
    project_id: uuid.UUID | None = None,
    label: str = "",
    telegram_token: str | None = None,
) -> tuple[BotChannel, str | None]:
    """Mint one channel for a bot you can run. Returns `(row, raw_token)` - the token shown
    ONCE (the ck_ ceremony), webhook kind only."""
    _require_enabled(kind)
    if kind == ChannelKind.EMAIL.value:
        raise ValidationFailed(_EMAIL_REFUSED, fields={"kind": "not yet"})
    if kind not in (ChannelKind.WEBHOOK.value, ChannelKind.TELEGRAM.value):
        raise ValidationFailed(
            f"the {kind} kind arrives in its own workstream",
            fields={"kind": "not yet"},
        )
    # A project bot is reachable through the project's ONE fetch path: a stranger who knows a
    # project id and a bot name gets a 404, never a door onto that bot (found in review - the
    # scope predicate alone is membership-blind). VIEWER, like opening a conversation.
    if project_id is not None:
        await Project.get_for(session, project_id, actor, min_role=Role.VIEWER)
    rows = await BotRecord.all_versions(session, bot_name, project_id=project_id, owner_actor_id=actor.id)
    if not rows:
        raise NotFound(f"no bot named {bot_name!r} in this scope")
    existing = await BotChannel.for_bot(session, actor.id, bot_name)
    if len(existing) >= MAX_CHANNELS_PER_BOT:
        raise Conflict(f"{bot_name} already has {MAX_CHANNELS_PER_BOT} channels - retire one first")

    raw: str | None = None
    address: str | None = None
    config: dict[str, Any] = {}
    config_enc: str | None = None
    enc_key_id: str | None = None
    if kind == ChannelKind.WEBHOOK.value:
        raw = new_channel_token()
    elif kind == ChannelKind.TELEGRAM.value:
        # BYO BotFather token: Fernet at rest (the connector-token posture),
        # verified live with getMe before the row exists - a wrong paste is a 422, not a
        # silently dead door. Poll mode drains it each minute; webhook mode is the operator's
        # public-HTTPS upgrade.
        if not telegram_token or ":" not in telegram_token:
            raise ValidationFailed(
                "the telegram kind needs the BotFather token (--telegram-token 123456:ABC...)",
                fields={"telegram_token": "required"},
            )
        me = await _telegram_get_me(telegram_token)
        from prismoid.operations import cipher

        config_enc = cipher.encrypt(telegram_token)
        enc_key_id = cipher.key_id()
        address = f"@{me}"
        config["telegram_offset"] = 0
    else:  # email: a real address, random-suffixed - guessable = spend-my-money
        domain = settings.channels.email_domain.strip().lower()  # `by_address` compares lowered
        if not domain:
            raise ValidationFailed(
                "the email kind needs PRISMOID_CHANNELS_EMAIL_DOMAIN set - the operator's move",
                fields={"kind": "email_domain unset"},
            )
        import secrets as _secrets

        address = f"{bot_name}-{_secrets.token_hex(3)}@{domain}"
        # Default-closed: the allowlist seeds with the owner's own login email, so emailing
        # someone's bot can never spend their money by default. Widening is an explicit dial.
        from prismoid.db.models.user import User

        user = await session.get(User, actor.id)
        if user is not None and user.email:
            config["allowlist"] = [user.email.lower()]

    row = BotChannel(
        id=uuid.uuid4(),
        owner_actor_id=actor.id,
        project_id=project_id,
        bot_name=bot_name,
        kind=kind,
        label=label.strip()[:120] or f"{bot_name} {kind}",
        address=address,
        token_hash=hash_token(raw) if raw else None,
        config=config,
        config_enc=config_enc,
        key_id=enc_key_id,
        budget_usd_per_turn=settings.channels.default_budget_usd_per_turn,
        daily_message_cap=settings.channels.daily_message_cap,
    )
    session.add(row)
    await session.commit()
    logfire.info("channel {id} minted for {bot} ({kind})", id=str(row.id), bot=bot_name, kind=kind)
    return row, raw


async def list_channels(
    session: AsyncSession, *, actor: Actor, bot_name: str | None = None
) -> list[BotChannel]:
    if bot_name:
        return await BotChannel.for_bot(session, actor.id, bot_name)
    return await BotChannel.for_owner(session, actor.id)


async def _owned(session: AsyncSession, actor: Actor, channel_id: uuid.UUID) -> BotChannel:
    row = await BotChannel.get_for(session, channel_id, actor.id)
    if row is None:
        raise NotFound("no such channel")  # existence is privileged, like a user memory
    return row


async def get_channel(session: AsyncSession, *, actor: Actor, channel_id: uuid.UUID) -> BotChannel:
    """The owner's read - 404s anyone else (existence is privileged)."""
    return await _owned(session, actor, channel_id)


async def rotate_token(
    session: AsyncSession, *, actor: Actor, channel_id: uuid.UUID
) -> tuple[BotChannel, str]:
    """Returns `(row, raw)` - the row the view serializes, so it never re-lists to find it."""
    row = await _owned(session, actor, channel_id)
    if row.kind != ChannelKind.WEBHOOK.value:
        raise ValidationFailed("only the webhook kind carries a bc_ token", fields={"kind": row.kind})
    raw = new_channel_token()
    row.token_hash = hash_token(raw)
    await session.commit()
    return row, raw


async def set_status(
    session: AsyncSession, *, actor: Actor, channel_id: uuid.UUID, status: ChannelStatus
) -> BotChannel:
    row = await _owned(session, actor, channel_id)
    row.status = status.value
    await session.commit()
    return row


async def update_channel(
    session: AsyncSession,
    *,
    actor: Actor,
    channel_id: uuid.UUID,
    input_mode: str | None = None,
    allowlist: list[str] | None = None,
    budget_usd_per_turn: float | None = None,
    daily_message_cap: int | None = None,
    work_grant: bool | None = None,
) -> BotChannel:
    row = await _owned(session, actor, channel_id)
    if input_mode is not None:
        if input_mode not in ("data", "instruction"):
            raise ValidationFailed("input_mode is data or instruction", fields={"input_mode": input_mode})
        row.input_mode = input_mode
    if allowlist is not None:
        config = dict(row.config or {})
        config["allowlist"] = [s.strip().lower() for s in allowlist if s.strip()][:50]
        row.config = config
    if budget_usd_per_turn is not None:
        row.budget_usd_per_turn = max(0.05, float(budget_usd_per_turn))
    if daily_message_cap is not None:
        row.daily_message_cap = max(1, int(daily_message_cap))
    if work_grant is not None:
        row.work_grant = bool(work_grant)
    await session.commit()
    return row


async def remove_channel(session: AsyncSession, *, actor: Actor, channel_id: uuid.UUID) -> None:
    row = await _owned(session, actor, channel_id)
    await session.delete(row)  # its conversations stay (channel_id SET NULL): a removed door keeps the talk
    await session.commit()


# ── the door (token-authenticated; auth exception #6) ─────────────────────────────────────────


async def authenticate(
    session: AsyncSession, channel_id: uuid.UUID, raw_token: str, *, read: bool = False
) -> BotChannel:
    """Wrong, revoked, and never-minted are ONE indistinguishable 401 (the fire door's law).
    The burst limiter counts AUTHENTICATED calls only - junk-token spray must not lock the
    real caller out of its own door. Reads ride their own looser window: a poll loop must
    not spend the send budget (polling the reply must never 429-lock the door)."""
    if not settings.channels.enabled:
        raise FeatureDisabled("channels are disabled on this instance")
    row = await session.get(BotChannel, channel_id)
    if row is None or row.token_hash is None:
        raise Unauthorized("that token does not open this channel")
    if not _hmac.compare_digest(row.token_hash, hash_token(raw_token)):
        raise Unauthorized("that token does not open this channel")
    _bind_channel(session, row)
    if await _owner_left_workspace(session, row):
        # A removed member's door closes in the workspace that removed them (as a routine's
        # SKIPPED_MEMBERSHIP does): the row says why, the caller hears a bad token - the
        # door is no oracle on who still belongs where.
        row.status = ChannelStatus.BROKEN.value
        row.last_error = _OWNER_LEFT
        await session.commit()
        raise Unauthorized("that token does not open this channel")
    if read:
        _read_limiter.hit(
            channel_id,
            per_minute=_READ_BURST_PER_MINUTE,
            message=f"this channel's feed takes {_READ_BURST_PER_MINUTE} polls per minute",
        )
    else:
        _inbound_limiter.hit(
            channel_id,
            per_minute=DEFAULT_BURST_PER_MINUTE,
            message=f"this channel takes {DEFAULT_BURST_PER_MINUTE} messages per minute",
        )
    return row


async def thread_of(
    session: AsyncSession, channel: BotChannel, thread_key: str, *, title: str | None = None
) -> Conversation:
    """The channel thread's conversation - found, or minted on first contact (the partial
    unique on `(channel_id, thread_key)` makes the race a constraint error, never a twin)."""
    key = thread_key[:120] or "main"
    existing = await Conversation.for_channel_thread(session, channel.id, key)
    if existing is not None:
        return existing
    row = Conversation(
        id=uuid.uuid4(),
        owner_actor_id=channel.owner_actor_id,
        project_id=channel.project_id,
        bot_name=channel.bot_name,
        title=(title or "").strip()[:200] or f"{channel.label or channel.kind} · {key[:24]}",
        source=ConversationSource.CHANNEL.value,
        channel_id=channel.id,
        thread_key=key,
    )
    session.add(row)
    await session.flush()
    return row


async def _sender_is_owner(session: AsyncSession, channel: BotChannel, sender: str) -> bool:
    """A webhook line is the owner's only when the owner said its lines are instructions
    (`input_mode`): the token is their integration, but what it relays may be anyone's words. An
    email sender is the owner when it is their login address; a Telegram sender is an outsider
    unless the allowlist is exactly them (the owner names themselves) - conservative, on purpose."""
    if channel.kind == ChannelKind.WEBHOOK.value:
        return channel.input_mode == "instruction"
    who = sender.strip().lower()
    if not who:
        return False
    if channel.kind == ChannelKind.EMAIL.value:
        from prismoid.db.models.user import User

        user = await session.get(User, channel.owner_actor_id)
        return user is not None and user.email.lower() == who
    allow = channel.allowlist()
    return len(allow) == 1 and allow[0] == who


async def accept_inbound(
    session: AsyncSession,
    channel: BotChannel,
    *,
    sender: str,
    body: str,
    thread_key: str = "main",
    provider_message_id: str | None = None,
    subject: str = "",
) -> InboundResult:
    """The one inbound path, every kind: the thread's conversation, then the door (or an honest
    refusal row). The caller has already authenticated by whatever the kind's credential is."""
    from prismoid.operations import chat as chat_ops

    found = await BotChannel.locked(session, channel.id)
    if found is None:
        raise NotFound("no such channel")
    locked: BotChannel = found  # declared once, so the refusal closure below reads a row, never None
    _bind_channel(session, locked)  # every kind's walk (webhook, email, telegram, the CLI) acts for it
    conv = await thread_of(session, locked, thread_key, title=subject)

    if provider_message_id and (dup := await Message.by_provider_id(session, conv.id, provider_message_id)):
        await session.commit()  # release the lock; the dedupe answered
        return InboundResult(
            seq=dup.seq,
            status="refused" if dup.verdict == Verdict.REFUSED.value else "accepted",
            verdict=dup.verdict,
            conversation_id=str(conv.id),
            task_id=str(dup.task_id) if dup.task_id else None,
            detail=dup.detail,
        )

    # An email sender arrives as RFC 5322 ("Dana <dana@x.com>"): the ADDRESS is the identity
    # the allowlist and the owner check compare (found in review: the display-name form refused
    # the owner's own mail).
    if locked.kind == ChannelKind.EMAIL.value:
        sender = (parseaddr(sender)[1] or sender).strip().lower()

    async def refuse(reason: str, *, broken: bool = False) -> InboundResult:
        # The refusal is a line on the record like any other (`chat.append`: the pointer rides
        # NOTIFY, the thread sees it live) - a USER row wearing the verdict and its reason.
        row = await chat_ops.append(
            session,
            conv,
            role=MessageRole.USER,
            text=body,
            sender=sender,
            verdict=Verdict.REFUSED,
            verdict_reason=reason,
            provider_message_id=provider_message_id,
            status="refused",
            detail=reason,
            meta={"subject": subject[:300]} if subject else {},
        )
        locked.last_error = reason[:500]
        if broken:
            locked.status = ChannelStatus.BROKEN.value  # the door's bot is gone: say so on the row
        await session.commit()
        logfire.info("channel {id} refused a message - {reason}", id=str(locked.id), reason=reason)
        return InboundResult(
            seq=row.seq,
            status="refused",
            verdict=Verdict.REFUSED.value,
            conversation_id=str(conv.id),
            detail=reason,
        )

    if (refusal := await _refusal(session, locked, sender=sender, body=body)) is not None:
        return await refuse(refusal.reason, broken=refusal.broken)

    locked.last_message_at = utcnow()
    try:
        outcome = await chat_ops.receive(
            session,
            conv,
            text=body,
            sender=sender,
            sender_is_owner=await _sender_is_owner(session, locked, sender),
            work_grant=locked.work_grant,
            budget_usd=float(locked.budget_usd_per_turn),
            provider_message_id=provider_message_id,
            meta={"received_at": utcnow().isoformat(), **({"subject": subject[:300]} if subject else {})},
        )
    except NotFound as exc:
        # The bot behind this door was archived or renamed (nothing else flips a channel to
        # BROKEN - found in review): a refused row + the status, never a raise the drain
        # replays every minute. The rollback expires (or expunges) the rows above: take the
        # lock again and re-mint the thread before writing the refusal.
        await session.rollback()
        relocked = await BotChannel.locked(session, channel.id)
        if relocked is None:
            raise NotFound("no such channel") from exc
        locked = relocked
        conv = await thread_of(session, locked, thread_key, title=subject)
        return await refuse(f"this channel's bot is gone ({exc})", broken=True)
    return InboundResult(
        seq=outcome.message.seq,
        status="refused" if outcome.verdict.verdict is Verdict.REFUSED else "accepted",
        verdict=outcome.verdict.verdict.value,
        conversation_id=str(conv.id),
        task_id=str(outcome.task.id) if outcome.task else None,
        detail=outcome.verdict.reason,
    )


class _Refusal(NamedTuple):
    reason: str
    broken: bool = False  # the door's bot or owner is gone: the channel says so on its row


async def _refusal(session: AsyncSession, locked: BotChannel, *, sender: str, body: str) -> _Refusal | None:
    """Why `accept_inbound` refuses this line, checked in order, or None to let it through. Reads only."""
    if locked.status != ChannelStatus.ACTIVE.value:
        return _Refusal(f"this channel is {locked.status}")
    owner = await session.get(Actor, locked.owner_actor_id)
    if owner is None or not owner.is_active():
        return _Refusal("this channel's owner is suspended - the door is closed")
    if await _owner_left_workspace(session, locked):
        return _Refusal(_OWNER_LEFT, broken=True)  # every kind: email, telegram, webhook
    if locked.kind == ChannelKind.EMAIL.value:
        return _Refusal(_EMAIL_REFUSED)  # a row made before the refusal: honest, mints nothing
    allow = locked.allowlist()
    if allow and sender.strip().lower() not in allow:
        return _Refusal("the sender is not on this channel's allowlist")
    if await Message.today_count_for_channel(session, locked.id) >= locked.daily_message_cap:
        return _Refusal(f"this channel's daily cap ({locked.daily_message_cap}) is spent")
    if not body.strip():
        return _Refusal("an empty message starts nothing")
    if await allowance.exhausted(session, locked.workspace_id):  # every line can cost a reply (ADR-103)
        return _Refusal("this workspace has reached its spending allowance")
    return None


async def authenticate_email_inbound(session: AsyncSession, *, secret: str, to: str) -> BotChannel:
    """The provider webhook's door (auth exception #7): one shared header secret (operator
    infrastructure, like the storage keys), then the address resolves the channel. Misses are
    404s - a prober learns nothing about which addresses exist."""
    if not settings.channels.enabled:
        raise FeatureDisabled("channels are disabled on this instance")
    expected = settings.channels.email_inbound_secret.get_secret_value()
    if not expected or not _hmac.compare_digest(expected, secret):
        raise Unauthorized("that secret does not open the inbound door")
    row = await BotChannel.by_address(session, ChannelKind.EMAIL.value, to)
    if row is None:
        raise NotFound("no such address")
    _bind_channel(session, row)
    return row


# A From address proves nothing until the inbound webhook carries a sender-authentication verdict
# (SPF/DKIM/DMARC): until then an email channel would let anyone write as its owner.
_EMAIL_REFUSED = "email channels are off until inbound mail carries a sender-authentication verdict"
_OWNER_LEFT = "this channel's owner is no longer a member of this workspace - the door is closed"


async def _owner_left_workspace(session: AsyncSession, channel: BotChannel) -> bool:
    """A channel speaks for its owner IN its workspace: once the owner is removed from
    that workspace, its doors stop taking lines and its bot stops replying and minting there."""
    return await WorkspaceMember.get(session, channel.workspace_id, channel.owner_actor_id) is None


def _bind_channel(session: AsyncSession, channel: BotChannel) -> None:
    """A channel's door acts for the CHANNEL's workspace: the bc_ token or the
    inbound address resolved to this row, and everything the line starts lives where it does."""
    tenant.bind(session, channel.workspace_id)


def email_thread_key(headers: dict[str, str], subject: str, message_id: str | None = None) -> str:
    """One email thread, one conversation. The key is the thread ROOT's Message-ID: the first
    entry of `References` (the root, by RFC 5322), else `In-Reply-To`, else the message's OWN
    id (it IS the root - every reply will name it). Only an id-less mail falls to its subject.
    Keying the first mail by subject and its reply by In-Reply-To would put
    one thread in two conversations."""
    folded = {k.lower(): v for k, v in headers.items()}  # header names are case-insensitive (RFC 5322)
    refs = (folded.get("references") or "").split()
    root = refs[0] if refs else (folded.get("in-reply-to") or "").split()[:1]
    root_id = root if isinstance(root, str) else (root[0] if root else "")
    root_id = root_id or (message_id or "").strip()
    if root_id:
        return hashlib.sha256(root_id.encode()).hexdigest()[:32]
    normalized = _SUBJECT_PREFIX.sub("", subject.lower()).strip()
    return hashlib.sha256(normalized.encode()).hexdigest()[:32] if normalized else "main"


# "Re: Re: Fwd: Budget" and "Budget" are one thread: every reply/forward prefix, however stacked.
_SUBJECT_PREFIX = re.compile(r"^(?:\s*(?:re|fwd?|aw|wg)\s*:)+", re.IGNORECASE)


async def _telegram_get_me(token: str) -> str:
    """One verify call at mint. httpx is already in the house; the token never
    rides a URL log - Telegram's API shape puts it in the path, so failures are sanitized."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=10.0) as http:
            resp = await http.get(f"{settings.channels.telegram_api_base}/bot{token}/getMe")
            data = resp.json()
    except Exception as exc:
        raise ValidationFailed("Telegram did not answer getMe - check the token") from exc
    if not data.get("ok"):
        raise ValidationFailed("Telegram refused that token", fields={"telegram_token": "refused"})
    return str(data["result"].get("username", "bot"))


def _telegram_token(channel: BotChannel) -> str | None:
    if not channel.config_enc:
        return None
    from prismoid.operations import cipher

    try:
        return cipher.decrypt(channel.config_enc)
    except Exception as exc:
        # a token that will not decrypt is a key rotation gone wrong, not a missing token
        logfire.warn(
            "channel {id}: the Telegram token does not decrypt ({kind})",
            id=str(channel.id),
            kind=type(exc).__name__,
        )
        return None


async def telegram_send(channel: BotChannel, chat_id: str, text: str) -> bool:
    """One outbound message, best-effort - a failed send costs the send, never the row."""
    import httpx

    token = _telegram_token(channel)
    if token is None:
        return False
    try:
        async with httpx.AsyncClient(timeout=10.0) as http:
            resp = await http.post(
                f"{settings.channels.telegram_api_base}/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text[:4000]},
            )
            return bool(resp.json().get("ok"))
    except Exception:
        logfire.exception("telegram send failed for channel {id}", id=str(channel.id))
        return False


async def telegram_drain(session: AsyncSession, channel: BotChannel) -> int:
    """Poll mode's minute drain: getUpdates past the stored offset, each message through the
    ONE inbound walk (allowlist, caps, fence - a Telegram sender is `@username` or the chat
    id). Returns how many messages were accepted."""
    import httpx

    token = _telegram_token(channel)
    if token is None:
        return 0
    offset = int((channel.config or {}).get("telegram_offset", 0) or 0)
    try:
        async with httpx.AsyncClient(timeout=25.0) as http:
            resp = await http.get(
                f"{settings.channels.telegram_api_base}/bot{token}/getUpdates",
                params={"offset": offset + 1, "timeout": 0, "allowed_updates": '["message"]'},
            )
            data = resp.json()
    except Exception:
        logfire.exception("telegram poll failed for channel {id}", id=str(channel.id))
        return 0
    if not data.get("ok"):
        return 0
    accepted = 0
    max_update = offset
    channel_id = channel.id  # a rollback below expires the row: hold the id, re-read the row
    for update in data.get("result", []):
        max_update = max(max_update, int(update.get("update_id", 0)))
        message = update.get("message") or {}
        text = str(message.get("text", "") or "")
        chat_id = str((message.get("chat") or {}).get("id", "") or "")
        # `@username` when the sender has one, else the chat id - the documented fallback
        # (found in review: a bare "@" was truthy, so it never fired).
        username = str((message.get("from") or {}).get("username") or "").strip()
        sender = f"@{username}" if username else chat_id
        if not text or not chat_id:
            continue
        try:
            await accept_inbound(
                session,
                channel,
                sender=sender,
                body=text,
                thread_key=chat_id,
                provider_message_id=f"tg-{update.get('update_id')}",
            )
            accepted += 1
        except Exception:
            # One update's failure costs that update, never the rest of the drain - and the
            # offset still advances below, so it is not replayed every minute (found in review).
            await session.rollback()
            logfire.exception(
                "telegram update {u} failed for channel {id}", u=update.get("update_id"), id=str(channel_id)
            )
            reloaded = await session.get(BotChannel, channel_id)
            if reloaded is None:
                return accepted
            channel = reloaded
    if max_update != offset:
        fresh = await session.get(BotChannel, channel_id)
        if fresh is not None:
            config = dict(fresh.config or {})
            config["telegram_offset"] = max_update
            fresh.config = config
            await session.commit()
    return accepted


# ── the reply path: the transport half (the conversation holds the row) ──────────────────────


async def transport_reply(session: AsyncSession, channel: BotChannel, conv: Conversation, text: str) -> bool:
    """Carry one bot line (or a delivery report) back out the channel it came in on. The
    email kind REPLIES AS THE BOT - its own from address, threading on the subject; Telegram
    sends to the chat the thread key names; the webhook kind's transport is the feed itself
    (the caller polls). Failures cost the send, never the row. Returns True when a transport
    actually carried it."""
    del session  # the row is already durable; this is the wire half only
    if channel.kind == ChannelKind.TELEGRAM.value and conv.thread_key not in ("", "main"):
        return await telegram_send(channel, conv.thread_key, text)
    if channel.kind == ChannelKind.EMAIL.value and conv.last_sender:
        from prismoid import mailer

        subject = (conv.title or "").strip() or f"Re: {channel.bot_name}"
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        return await mailer.send(
            to=conv.last_sender,
            subject=subject,
            text=text,
            from_address=channel.address,
            reply_to=channel.address,
        )
    return False


async def feed(
    session: AsyncSession, channel: BotChannel, *, thread_key: str = "main", since: int = 0, limit: int = 100
) -> list[Message]:
    """The poll feed for one thread of the channel: everything after `since`, oldest first."""
    conv = await Conversation.for_channel_thread(session, channel.id, thread_key[:120] or "main")
    if conv is None:
        return []
    return await Message.since(session, conv.id, since=since, limit=limit)
