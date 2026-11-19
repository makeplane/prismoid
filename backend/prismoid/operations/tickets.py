# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Screen and display tickets (ADR-065): signed, single-use,
30-second admissions - never credentials, and never stored.

Two kinds, one key, two domains:

    st_<base64url(claims)>.<base64url(Ed25519 signature)>   the browser's screen socket
        claims {task, actor, cred, cred_kind, conn, iat, exp, jti, kid}, signed over
        b"percy-screen-v1\\n" + claims
    dt_<base64url(claims)>.<base64url(Ed25519 signature)>   sandbar's display route
        claims {sandbox, iat, exp, jti, kid}, signed over b"sandbar-display-v1\\n" + claims

The claims are compact JSON with sorted keys; base64url carries no padding; `iat`/`exp` are
integer Unix seconds, `exp - iat` at most `ticket_ttl_s` (30); `jti` is 16 random bytes in hex.
The domain strings keep either kind from being replayed as the other. sandbar verifies `dt_`
in Go against the public half (`public_keys`, SANDBAR_DISPLAY_KEYS) and never this module.

`peek_*` checks a ticket without spending it (the door looks before it accepts the socket);
`redeem` spends its `jti` after the accept. The spent cache lives in this process only, so
with several API replicas one ticket could be redeemed once on each within its 30 s - bound to
one live credential row and one connection, accepted at design-partner scale (Valkey
closes it). A ticket issued before this process started PLUS the skew is refused: a minting clock up
to `SKEW_S` ahead stamps a ticket the previous process
may have spent with an `iat` up to `SKEW_S` past this start, so a restart cannot reopen a
replay window. The price: a ticket minted in a process's first seconds is refused once, and
the client mints another.

The key is `settings.api.screen_signing_key` ("<kid>:<base64 seed>"); unset (dev), each process
mints its own, kid `dev-<pid>` - sound because dev runs one API process, and staging and
production refuse to boot without the setting while computer use is on.
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Final

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from prismoid.conf import settings
from prismoid.schemas.principal import CredKind
from prismoid.schemas.screen import CONN_PATTERN

SCREEN_PREFIX: Final = "st_"
DISPLAY_PREFIX: Final = "dt_"
SKEW_S: Final = 5
# When this process began - read at import (the API imports this module at boot, before any
# ticket it mints) and looked up at every check, so a test can pin it.
PROCESS_STARTED: float = time.time()

_SCREEN_DOMAIN: Final = b"percy-screen-v1\n"
_DISPLAY_DOMAIN: Final = b"sandbar-display-v1\n"
_TICKET_MAX: Final = 2048  # characters; a real ticket is under 500
_B64URL: Final = re.compile(r"[A-Za-z0-9_-]+")
_CONN: Final = re.compile(CONN_PATTERN)  # presence's connection id
_JTI: Final = re.compile(r"[0-9a-f]{32}")
_SCREEN_KEYS: Final = frozenset({"task", "actor", "cred", "cred_kind", "conn", "iat", "exp", "jti", "kid"})
_DISPLAY_KEYS: Final = frozenset({"sandbox", "iat", "exp", "jti", "kid"})

# jti → the moment after which no peek can pass for its ticket (exp + skew): pruned past it.
_spent: dict[str, float] = {}


class TicketRefused(Exception):
    """Why a ticket does not admit: malformed | signature | expired | lifetime | stale |
    replayed | mismatch | unknown_kid. The socket closes 4401 whatever the reason; the reason
    is for the log line and the tests, never for the client."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ScreenClaims:
    task: uuid.UUID
    actor: uuid.UUID
    cred: uuid.UUID  # the credential ROW the minting request authenticated with, never its token
    cred_kind: CredKind
    conn: str
    iat: int
    exp: int
    jti: str
    kid: str


@dataclass(frozen=True, slots=True)
class DisplayClaims:
    sandbox: str
    iat: int
    exp: int
    jti: str
    kid: str


# ── the key ───────────────────────────────────────────────────────────────────────────────


@lru_cache(maxsize=1)
def _signing_key() -> tuple[str, Ed25519PrivateKey]:
    configured = settings.api.screen_key()
    if configured is None:
        return f"dev-{os.getpid()}", Ed25519PrivateKey.generate()
    kid, seed = configured
    return kid, Ed25519PrivateKey.from_private_bytes(seed)


def public_keys() -> dict[str, str]:
    """kid → the raw Ed25519 public key in standard base64: what sandbar's
    SANDBAR_DISPLAY_KEYS holds (`<kid>:<base64>`), so it can verify and never mint."""
    kid, key = _signing_key()
    return {kid: base64.b64encode(key.public_key().public_bytes_raw()).decode()}


# ── minting ───────────────────────────────────────────────────────────────────────────────


def mint_screen(
    *,
    task: uuid.UUID,
    actor: uuid.UUID,
    cred: uuid.UUID,
    cred_kind: CredKind,
    conn: str,
    now: float | None = None,
) -> str:
    """A screen ticket for one connection of one actor to one task's screen. `conn` is validated
    here too, so a ticket is never minted that its own peek would refuse."""
    if not _CONN.fullmatch(conn):
        raise ValueError("conn is not a connection id")
    return _issue(
        SCREEN_PREFIX,
        _SCREEN_DOMAIN,
        {"task": str(task), "actor": str(actor), "cred": str(cred), "cred_kind": cred_kind, "conn": conn}
        | _window(now),
    )


def mint_display(sandbox: str, *, now: float | None = None) -> str:
    """A display ticket for one sandbox's display route, for sandbar to verify."""
    return _issue(DISPLAY_PREFIX, _DISPLAY_DOMAIN, {"sandbox": sandbox} | _window(now))


def _window(now: float | None) -> dict[str, Any]:
    iat = int(_clock(now))
    return {"iat": iat, "exp": iat + settings.computer_use.ticket_ttl_s, "jti": secrets.token_hex(16)}


def _issue(prefix: str, domain: bytes, claims: Mapping[str, Any]) -> str:
    """The one signer. The claims travel as the exact bytes that were signed."""
    kid, key = _signing_key()
    body = json.dumps({**claims, "kid": kid}, separators=(",", ":"), sort_keys=True).encode()
    return f"{prefix}{_b64(body)}.{_b64(key.sign(domain + body))}"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _clock(now: float | None) -> float:
    return time.time() if now is None else now


# ── checking ──────────────────────────────────────────────────────────────────────────────


def peek_screen(ticket: str, *, task: uuid.UUID, now: float | None = None) -> ScreenClaims:
    """Check a screen ticket WITHOUT spending it: its shape, its key, its signature under the
    screen domain, its lifetime and expiry (±SKEW_S), that it is not older than this process,
    that it is unspent, and that it names `task`. TicketRefused otherwise."""
    raw = _open(ticket, SCREEN_PREFIX, _SCREEN_DOMAIN)
    if frozenset(raw) != _SCREEN_KEYS:
        raise TicketRefused("malformed")
    claims = ScreenClaims(
        task=_uuid(raw["task"]),
        actor=_uuid(raw["actor"]),
        cred=_uuid(raw["cred"]),
        cred_kind=_cred_kind(raw["cred_kind"]),
        conn=_matching(_CONN, raw["conn"]),
        iat=_int(raw["iat"]),
        exp=_int(raw["exp"]),
        jti=_matching(_JTI, raw["jti"]),
        kid=str(raw["kid"]),
    )
    _admit(claims.jti, claims.exp, _clock(now), iat=claims.iat)
    if claims.task != task:
        raise TicketRefused("mismatch")
    return claims


def peek_display(ticket: str, *, sandbox: str, now: float | None = None) -> DisplayClaims:
    """`peek_screen` for a display ticket and the sandbox it must name (sandbar's own check,
    kept here so the two halves of the protocol are tested against each other)."""
    raw = _open(ticket, DISPLAY_PREFIX, _DISPLAY_DOMAIN)
    if frozenset(raw) != _DISPLAY_KEYS or not isinstance(raw["sandbox"], str) or not raw["sandbox"]:
        raise TicketRefused("malformed")
    claims = DisplayClaims(
        sandbox=raw["sandbox"],
        iat=_int(raw["iat"]),
        exp=_int(raw["exp"]),
        jti=_matching(_JTI, raw["jti"]),
        kid=str(raw["kid"]),
    )
    _admit(claims.jti, claims.exp, _clock(now), iat=claims.iat)
    if claims.sandbox != sandbox:
        raise TicketRefused("mismatch")
    return claims


def redeem(jti: str, exp: int, *, now: float | None = None) -> None:
    """Spend a peeked ticket's `jti`: the same admission a peek ran, then the write. Synchronous,
    with no await between the check and the write, so two sockets racing one ticket on this
    event loop cannot both win. Spent entries are pruned once no peek could pass for their
    ticket."""
    at = _clock(now)
    for spent, until in list(_spent.items()):
        if until < at:
            del _spent[spent]
    _admit(jti, exp, at)
    _spent[jti] = exp + SKEW_S


def _admit(jti: str, exp: int, at: float, *, iat: int | None = None) -> None:
    """The one admission check, in the documented order: lifetime → expiry → staleness → spent.
    A peek passes the ticket's `iat` and gets all four; a redeem follows a peek that checked the
    window already, so it re-asks only what time and other sockets can change: expiry, spent."""
    if iat is not None and (not 0 < exp - iat <= settings.computer_use.ticket_ttl_s or iat > at + SKEW_S):
        raise TicketRefused("lifetime")  # not a window this API mints
    if at > exp + SKEW_S:
        raise TicketRefused("expired")
    if iat is not None and iat < PROCESS_STARTED + SKEW_S:
        raise TicketRefused("stale")  # it may have been spent before this process began
    if jti in _spent:
        raise TicketRefused("replayed")


def _open(ticket: str, prefix: str, domain: bytes) -> dict[str, Any]:
    """Shape → claims JSON → kid → signature over `domain + claims`, in that order. The JSON is
    parsed before the signature is checked only to read the kid; the size cap bounds it."""
    if len(ticket) > _TICKET_MAX or not ticket.startswith(prefix):
        raise TicketRefused("malformed")
    body_text, dot, sig_text = ticket[len(prefix) :].partition(".")
    if not dot:
        raise TicketRefused("malformed")
    body, sig = _unb64(body_text), _unb64(sig_text)
    try:
        claims = json.loads(body)
    except (ValueError, RecursionError):
        raise TicketRefused("malformed") from None
    if not isinstance(claims, dict):
        raise TicketRefused("malformed")
    kid, key = _signing_key()
    if claims.get("kid") != kid:
        raise TicketRefused("unknown_kid")
    try:
        key.public_key().verify(sig, domain + body)
    except InvalidSignature:
        raise TicketRefused("signature") from None
    return claims


def _unb64(text: str) -> bytes:
    if not _B64URL.fullmatch(text):
        raise TicketRefused("malformed")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except ValueError:  # binascii.Error: a length no base64 has
        raise TicketRefused("malformed") from None


def _uuid(value: object) -> uuid.UUID:
    if isinstance(value, str):
        try:
            parsed = uuid.UUID(value)
        except ValueError:
            parsed = None
        if parsed is not None and str(parsed) == value:
            return parsed
    raise TicketRefused("malformed")


def _cred_kind(value: object) -> CredKind:
    if isinstance(value, str) and value in CredKind:
        return CredKind(value)
    raise TicketRefused("malformed")


def _int(value: object) -> int:
    if type(value) is not int:  # a bool is an int to isinstance, never to a ticket
        raise TicketRefused("malformed")
    return value


def _matching(pattern: re.Pattern[str], value: object) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise TicketRefused("malformed")
    return value


def _reset_for_tests() -> None:  # pyright: ignore[reportUnusedFunction]  # the suites call it
    """A fresh key (the settings may have changed) and an empty spent cache."""
    _signing_key.cache_clear()
    _spent.clear()


__all__ = [
    "DISPLAY_PREFIX",
    "PROCESS_STARTED",
    "SCREEN_PREFIX",
    "SKEW_S",
    "CredKind",
    "DisplayClaims",
    "ScreenClaims",
    "TicketRefused",
    "mint_display",
    "mint_screen",
    "peek_display",
    "peek_screen",
    "public_keys",
    "redeem",
]
