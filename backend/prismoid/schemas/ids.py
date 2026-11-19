# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The identifiers, named.

`NewType` rather than a bare alias: a `ProjectId` where a `TaskId` belongs is the kind of bug that
type-checks perfectly and corrupts a ledger quietly. The cost is one explicit call at the point
of construction - which is the point where you should be thinking about it anyway.

`skill_name` is the Agent Skills spec's name rule. A skill's name becomes a directory in a Machine
(`/skills/<name>`) and a folder in an export, so the grammar keeps `x;id`, `../x` and `$(id)` out.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from typing import Final, NewType
from uuid import UUID, uuid4

ActorId = NewType("ActorId", UUID)
UserId = NewType("UserId", UUID)  # same value as the Actor's - users.actor_id is PK/FK
ProjectId = NewType("ProjectId", UUID)
ConversationId = NewType("ConversationId", UUID)  # a bot conversation - talk, never work
TaskId = NewType("TaskId", UUID)
StepId = NewType("StepId", UUID)
GateId = NewType("GateId", UUID)
ArtifactId = NewType("ArtifactId", UUID)
CaptureId = NewType("CaptureId", UUID)
InboxItemId = NewType("InboxItemId", UUID)
UsageEventId = NewType("UsageEventId", UUID)
MemoryId = NewType("MemoryId", UUID)
ConnectorId = NewType("ConnectorId", UUID)

# A skill is addressed by (name, version) - `report@3` - or by `report@draft` (author-only).
SkillName = NewType("SkillName", str)

SKILL_NAME_MAX: Final = 64
_SKILL_NAME = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")


def skill_name(text: str) -> SkillName:
    """`text` as a `SkillName`, or a `ValueError` saying what the rule is - the one door a name
    from outside (a request, an imported bundle, a stored row about to be mounted) walks through."""
    if len(text) > SKILL_NAME_MAX or not _SKILL_NAME.fullmatch(text):
        raise ValueError(
            f"{text!r} is not a legal skill name: 1-{SKILL_NAME_MAX} lowercase letters, digits and "
            "single hyphens, none leading or trailing"
        )
    return SkillName(text)


# Agents are addressed by their registry name ("writer", "maker") - roles, never models.
AgentName = NewType("AgentName", str)

# A bot - a user-authored agent - is addressed like a skill: `(name, version)`, `reviewer@3`, or
# `reviewer@draft` (author-only). It compiles to an `AgentDef` and is therefore reachable by a
# plan step, so it shares `AgentName`'s namespace: a built-in always wins a collision, and the
# reserved set is `RESERVED_BOT_NAMES` in `schemas/bots.py`.
BotName = NewType("BotName", str)


def new_task_id() -> TaskId:
    return TaskId(uuid4())


def new_step_id() -> StepId:
    return StepId(uuid4())


# ── opaque tokens ─────────────────────────────────────────────────────────────────────────────
# Both are shown once, stored only as a hash, and revocable in Postgres. The prefix is what
# `require_actor` dispatches on, and what the Logfire scrubber greps for.

SESSION_TOKEN_PREFIX: Final = "cs_"  # a human's login session
API_KEY_PREFIX: Final = "ck_"  # an actor's programmatic key
_TOKEN_BYTES: Final = 32


CONNECTOR_STATE_PREFIX: Final = "cn_"  # a connector OAuth flow's state parameter
OAUTH_BINDING_PREFIX: Final = "cb_"  # the browser that started a connector OAuth flow: an
# HttpOnly cookie, stored only as its hash in the flow; the callback refuses any other browser.
OAUTH_BINDING_COOKIE: Final = "prismoid_oauth_binding"
CONNECTOR_RETURN_PREFIX: Final = "ca_"  # a connector OAuth flow handed back to the phone app:
# single-use, minutes-lived, stored only as its hash in the flow; finishing also needs the app's secret.

PASSWORD_RESET_PREFIX: Final = "cr_"  # a forgotten-password reset code, emailed
EMAIL_VERIFICATION_PREFIX: Final = (
    "ev_"  # an address-verification code, emailed (its link carries it in the fragment)
)

FIRE_TOKEN_PREFIX: Final = "rt_"  # a routine's inbound fire token: starts runs,
# and NOTHING else - it cannot read, edit, steer, or list. Long-lived until rotated/revoked.

CHANNEL_TOKEN_PREFIX: Final = "bc_"  # a bot channel's inbound token: starts and
# reads that channel's turns, and NOTHING else - the rt_ grammar, one lane over.

WORKSPACE_INVITE_PREFIX: Final = "oi_"  # a workspace invite code, emailed: single-use, hashed, TTL'd
PROJECT_INVITE_PREFIX: Final = "si_"  # a Project join link: hashed, TTL'd, single-use, owner-minted

STREAM_TICKET_PREFIX: Final = "et_"  # an event-stream ticket: admits ONE stream of one task or
# conversation, once, within seconds; hashed at rest, bound to the credential row that minted it.

SHARE_SLUG_PREFIX: Final = "bt_"  # a bot template's share slug. NOT a credential:
# it gates only content its author pressed Share on, so it is stored PLAINTEXT (it must stay
# re-displayable in the author's list forever) - the capability is the 128 bits, the misses
# are 404s, and unpublishing kills it.


def new_session_token() -> str:
    return f"{SESSION_TOKEN_PREFIX}{secrets.token_urlsafe(_TOKEN_BYTES)}"


def new_password_reset_code() -> str:
    """The one-time code a locked-out user gets by email: their whole authentication for
    `POST /v1/auth/reset-password`, so it is the full 32-byte `secrets` family - prefixed for
    the scrubber, stored only as `hash_token(code)`, single-use, and TTL-bound."""
    return f"{PASSWORD_RESET_PREFIX}{secrets.token_urlsafe(_TOKEN_BYTES)}"


def new_email_verification_code() -> str:
    """The one-time code that proves an address: the `cr_` family (32 bytes of `secrets`,
    prefixed for the scrubber, stored only as `hash_token(code)`, single-use, TTL-bound)."""
    return f"{EMAIL_VERIFICATION_PREFIX}{secrets.token_urlsafe(_TOKEN_BYTES)}"


def new_connector_state() -> str:
    """The OAuth `state` for one authorize round trip: the callback's whole authentication.

    Same family as the session token - 32 bytes of `secrets`-grade randomness, prefixed for the
    scrubber, stored only as `hash_token(state)`, and **single-use** (the row claim clears it)."""
    return f"{CONNECTOR_STATE_PREFIX}{secrets.token_urlsafe(_TOKEN_BYTES)}"


def new_connector_return_code() -> str:
    """The one-time code the callback hands the phone app with an app sign-in: the 32-byte
    `secrets` family, prefixed for the scrubber, stored only as `hash_token(code)` in the flow."""
    return f"{CONNECTOR_RETURN_PREFIX}{secrets.token_urlsafe(_TOKEN_BYTES)}"


def new_oauth_binding() -> str:
    """The connector OAuth browser binding (the cookie's value): the 32-byte `secrets` family,
    prefixed for the scrubber, stored only as `hash_token(binding)` inside the sealed flow."""
    return f"{OAUTH_BINDING_PREFIX}{secrets.token_urlsafe(_TOKEN_BYTES)}"


def new_workspace_invite_code() -> str:
    """A workspace invite's whole authentication: the cr_ ceremony verbatim -
    32-byte `secrets` family, prefixed for the scrubber, stored only as `hash_token(code)`."""
    return f"{WORKSPACE_INVITE_PREFIX}{secrets.token_urlsafe(_TOKEN_BYTES)}"


def new_project_invite_code() -> str:
    """A Project join link's whole authentication: the oi_ ceremony - 32-byte `secrets`
    family, prefixed for the scrubber, stored only as `hash_token(code)`."""
    return f"{PROJECT_INVITE_PREFIX}{secrets.token_urlsafe(_TOKEN_BYTES)}"


def new_api_key() -> str:
    return f"{API_KEY_PREFIX}{secrets.token_urlsafe(_TOKEN_BYTES)}"


def new_channel_token() -> str:
    """A channel's whole inbound authentication: the rt_ ceremony verbatim -
    32-byte `secrets` family, prefixed for the scrubber, stored only as `hash_token(raw)`,
    shown once, rotation supersedes. It authenticates the CHANNEL, never a person: it can
    start and read that one channel's turns and nothing else."""
    return f"{CHANNEL_TOKEN_PREFIX}{secrets.token_urlsafe(_TOKEN_BYTES)}"


def new_stream_ticket() -> str:
    """A browser's admission to one event stream: the 32-byte `secrets` family, prefixed for the
    scrubber, stored only as `hash_token(ticket)`, spent by the stream that redeems it. It rides
    the stream URL because an EventSource cannot set a header; the session token never does."""
    return f"{STREAM_TICKET_PREFIX}{secrets.token_urlsafe(_TOKEN_BYTES)}"


def new_share_slug() -> str:
    """A template link's capability: 16 bytes ≈ 128 bits, enough that enumeration
    is a non-event behind the per-IP throttle. Shorter than the credential family on purpose -
    it is a URL people paste in chat, and it authenticates no mutation."""
    return f"{SHARE_SLUG_PREFIX}{secrets.token_urlsafe(16)}"


def new_fire_token() -> str:
    """A routine's whole inbound authentication: same 32-byte `secrets` family,
    prefixed for the scrubber, stored only as `hash_token(raw)`, shown once, rotation
    supersedes. Deliberately NOT an Actor credential - the fire door authenticates the
    ROUTINE, and the payload it carries is data behind the fence, never instruction."""
    return f"{FIRE_TOKEN_PREFIX}{secrets.token_urlsafe(_TOKEN_BYTES)}"


def hash_token(raw: str) -> str:
    """The one hashing function for both token kinds. Hash at the door - the plaintext is shown
    to the client once and is never written anywhere.

    SHA-256, not argon2, and deliberately: these are 32 bytes of `secrets`-grade randomness, not
    passwords. There is no dictionary to attack, so a slow hash would buy nothing and cost a
    round of it on **every authenticated request** - performance budget spent for no security.
    Passwords are the opposite case, and get argon2id.
    """
    return hashlib.sha256(raw.encode()).hexdigest()
