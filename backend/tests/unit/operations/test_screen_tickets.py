# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Screen and display tickets: sign, verify, peek,
redeem, replay, domain separation, lifetime, skew, staleness and the key - and the credential a
screen ticket names. Pure: no database, no clock (every call takes `now`)."""

from __future__ import annotations

import base64
import json
import math
import os
import re
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import SecretStr

from prismoid.api.deps import credential
from prismoid.conf import settings
from prismoid.db.models.actor import Actor
from prismoid.errors import Forbidden, Unauthorized
from prismoid.operations import tickets
from prismoid.operations.tickets import (
    DISPLAY_PREFIX,
    PROCESS_STARTED,
    SCREEN_PREFIX,
    SKEW_S,
    DisplayClaims,
    ScreenClaims,
    TicketRefused,
    mint_display,
    mint_screen,
    peek_display,
    peek_screen,
    public_keys,
    redeem,
)
from prismoid.schemas.principal import Credential, CredKind, Principal

SEED = bytes(range(32))
NOW = PROCESS_STARTED + 100.0
TASK, ACTOR, CRED = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
CONN = "tab-0001-abcd"
SCREEN_DOMAIN = b"percy-screen-v1\n"
DISPLAY_DOMAIN = b"sandbar-display-v1\n"


def key(kid: str, seed: bytes = SEED) -> SecretStr:
    return SecretStr(f"{kid}:{base64.b64encode(seed).decode()}")


@pytest.fixture(autouse=True)
def screen_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(settings.api, "screen_signing_key", key("k1"))
    monkeypatch.setattr(settings.computer_use, "ticket_ttl_s", 30)
    tickets._reset_for_tests()  # pyright: ignore[reportPrivateUsage]
    yield
    tickets._reset_for_tests()  # pyright: ignore[reportPrivateUsage]


def screen(now: float = NOW, **over: Any) -> str:
    fields: dict[str, Any] = {
        "task": TASK,
        "actor": ACTOR,
        "cred": CRED,
        "cred_kind": "session",
        "conn": CONN,
    }
    fields.update(over)
    return mint_screen(now=now, **fields)


def signed(prefix: str, domain: bytes, claims: dict[str, Any]) -> str:
    """Any claims, signed with the configured key - how a test builds a ticket the API never mints."""
    return tickets._issue(prefix, domain, claims)  # pyright: ignore[reportPrivateUsage]


def screen_claims(**over: Any) -> dict[str, Any]:
    claims: dict[str, Any] = {
        "task": str(TASK),
        "actor": str(ACTOR),
        "cred": str(CRED),
        "cred_kind": "session",
        "conn": CONN,
        "iat": int(NOW),
        "exp": int(NOW) + 30,
        "jti": "0" * 32,
    }
    claims.update(over)
    return claims


def parts(ticket: str) -> tuple[str, bytes, bytes]:
    def unb64(text: str) -> bytes:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))

    body, _, sig = ticket[3:].partition(".")
    return ticket[:3], unb64(body), unb64(sig)


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def refused(reason: str, call: Callable[[], object]) -> None:
    with pytest.raises(TicketRefused) as caught:
        call()
    assert caught.value.reason == reason


# ── round trips ───────────────────────────────────────────────────────────────────────────


def test_a_screen_ticket_round_trips_and_a_peek_never_spends_it() -> None:
    ticket = screen()
    assert ticket.startswith(SCREEN_PREFIX) and "=" not in ticket
    claims = peek_screen(ticket, task=TASK, now=NOW + 1)
    assert claims == ScreenClaims(
        task=TASK,
        actor=ACTOR,
        cred=CRED,
        cred_kind=CredKind.SESSION,
        conn=CONN,
        iat=int(NOW),
        exp=int(NOW) + 30,
        jti=claims.jti,
        kid="k1",
    )
    assert re.fullmatch(r"[0-9a-f]{32}", claims.jti)
    assert peek_screen(ticket, task=TASK, now=NOW + 2) == claims  # still unspent
    assert peek_screen(screen(), task=TASK, now=NOW).jti != claims.jti  # every mint is new


def test_redeem_spends_a_ticket_once() -> None:
    ticket = screen()
    claims = peek_screen(ticket, task=TASK, now=NOW)
    redeem(claims.jti, claims.exp, now=NOW + 1)
    refused("replayed", lambda: peek_screen(ticket, task=TASK, now=NOW + 2))
    refused("replayed", lambda: redeem(claims.jti, claims.exp, now=NOW + 2))


def test_a_display_ticket_round_trips_for_its_sandbox() -> None:
    ticket = mint_display("sbx-0f3a", now=NOW)
    assert ticket.startswith(DISPLAY_PREFIX)
    claims = peek_display(ticket, sandbox="sbx-0f3a", now=NOW)
    assert claims == DisplayClaims(
        sandbox="sbx-0f3a", iat=int(NOW), exp=int(NOW) + 30, jti=claims.jti, kid="k1"
    )
    redeem(claims.jti, claims.exp, now=NOW)
    refused("replayed", lambda: peek_display(ticket, sandbox="sbx-0f3a", now=NOW))


def test_the_wire_form_is_canonical_claims_signed_under_each_domain() -> None:
    public = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_keys()["k1"]))
    expected = Ed25519PrivateKey.from_private_bytes(SEED).public_key().public_bytes_raw()
    assert public.public_bytes_raw() == expected
    for ticket, domain, other, keys in (
        (screen(), SCREEN_DOMAIN, DISPLAY_DOMAIN, {"task", "actor", "cred", "cred_kind", "conn"}),
        (mint_display("sbx-1", now=NOW), DISPLAY_DOMAIN, SCREEN_DOMAIN, {"sandbox"}),
    ):
        _, body, sig = parts(ticket)
        assert body == json.dumps(json.loads(body), separators=(",", ":"), sort_keys=True).encode()
        assert set(json.loads(body)) == keys | {"iat", "exp", "jti", "kid"}
        public.verify(sig, domain + body)  # raises if the domain string is not exactly this
        with pytest.raises(InvalidSignature):
            public.verify(sig, other + body)


# ── refusals ──────────────────────────────────────────────────────────────────────────────


def test_a_tampered_claim_or_signature_is_refused() -> None:
    prefix, body, sig = parts(screen())
    forged = body.replace(CONN.encode(), b"tab-9999-abcd")
    refused("signature", lambda: peek_screen(f"{prefix}{b64(forged)}.{b64(sig)}", task=TASK, now=NOW))
    flipped = bytes([sig[0] ^ 1]) + sig[1:]
    refused("signature", lambda: peek_screen(f"{prefix}{b64(body)}.{b64(flipped)}", task=TASK, now=NOW))


def test_neither_kind_passes_as_the_other() -> None:
    display, shown = mint_display("sbx-1", now=NOW), screen()
    refused("malformed", lambda: peek_screen(display, task=TASK, now=NOW))  # the prefix
    refused("malformed", lambda: peek_display(shown, sandbox="sbx-1", now=NOW))
    # Relabelled, the signature is checked under the wrong domain string and fails.
    refused("signature", lambda: peek_screen(SCREEN_PREFIX + display[3:], task=TASK, now=NOW))
    refused("signature", lambda: peek_display(DISPLAY_PREFIX + shown[3:], sandbox="sbx-1", now=NOW))


@pytest.mark.parametrize(
    "ticket",
    ["", "st_", "st_abc", "st_ab+c.def", "st_abc.de=f", "st_a.b.c", "st_" + "a" * 3000 + ".b", "ck_abc.def"],
)
def test_a_malformed_ticket_is_refused(ticket: str) -> None:
    refused("malformed", lambda: peek_screen(ticket, task=TASK, now=NOW))


@pytest.mark.parametrize(
    "claims",
    [
        screen_claims(iat=str(int(NOW))),  # not an integer
        screen_claims(exp=True),  # a bool is not a time
        screen_claims(extra=1),  # a claim the format does not have
        screen_claims(cred_kind="password"),
        screen_claims(conn="short"),
        screen_claims(conn="tab-0001-abcd\n"),  # the pattern's `$` never admits a trailing newline
        screen_claims(task="not-a-uuid"),
        screen_claims(task=str(TASK).upper()),  # not the canonical form
        screen_claims(jti="xyz"),
    ],
)
def test_signed_claims_of_the_wrong_shape_are_refused(claims: dict[str, Any]) -> None:
    refused(
        "malformed", lambda: peek_screen(signed(SCREEN_PREFIX, SCREEN_DOMAIN, claims), task=TASK, now=NOW)
    )


def test_a_non_json_or_non_object_body_is_refused() -> None:
    _, _, sig = parts(screen())
    for body in (b"not json", b"[1, 2]"):
        refused("malformed", lambda body=body: peek_screen(f"st_{b64(body)}.{b64(sig)}", task=TASK, now=NOW))


def test_expiry_allows_five_seconds_of_skew_and_no_more() -> None:
    claims = peek_screen(screen(), task=TASK, now=NOW)
    ticket = screen()
    assert peek_screen(ticket, task=TASK, now=claims.exp + SKEW_S).kid == "k1"
    refused("expired", lambda: peek_screen(ticket, task=TASK, now=claims.exp + SKEW_S + 0.001))
    refused("expired", lambda: redeem(claims.jti, claims.exp, now=claims.exp + SKEW_S + 0.001))


def test_a_lifetime_over_the_ttl_or_backwards_or_from_the_future_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(NOW)
    for iat, exp in ((now, now + 31), (now, now), (now, now - 1)):
        ticket = signed(SCREEN_PREFIX, SCREEN_DOMAIN, screen_claims(iat=iat, exp=exp))
        refused("lifetime", lambda ticket=ticket: peek_screen(ticket, task=TASK, now=now))
    ahead = signed(SCREEN_PREFIX, SCREEN_DOMAIN, screen_claims(iat=now + SKEW_S, exp=now + SKEW_S + 30))
    assert peek_screen(ahead, task=TASK, now=now).iat == now + SKEW_S  # a clock 5 s ahead is fine
    beyond = signed(SCREEN_PREFIX, SCREEN_DOMAIN, screen_claims(iat=now + SKEW_S + 1, exp=now + SKEW_S + 31))
    refused("lifetime", lambda: peek_screen(beyond, task=TASK, now=now))
    monkeypatch.setattr(settings.computer_use, "ticket_ttl_s", 10)
    refused(
        "lifetime",
        lambda: peek_screen(signed(SCREEN_PREFIX, SCREEN_DOMAIN, screen_claims()), task=TASK, now=now),
    )
    shorter = peek_screen(screen(), task=TASK, now=NOW)
    assert shorter.exp - shorter.iat == 10


def test_a_ticket_minted_before_this_process_started_plus_the_skew_is_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clock up to SKEW_S ahead stamps a pre-restart ticket up to SKEW_S past this process's
    start, so the earliest admitted `iat` is the start PLUS the skew: a ticket minted
    in the first seconds is refused once, and the client mints another."""
    started = 1_800_000_000.25  # pinned, and fractional, so the edge is exact
    monkeypatch.setattr(tickets, "PROCESS_STARTED", started)
    after = started + SKEW_S + 1
    assert peek_screen(screen(now=after), task=TASK, now=after).iat == int(after)
    edge = math.ceil(started + SKEW_S)  # the earliest iat this process accepts
    at_edge = signed(SCREEN_PREFIX, SCREEN_DOMAIN, screen_claims(iat=edge, exp=edge + 30))
    assert peek_screen(at_edge, task=TASK, now=edge + 1).iat == edge
    before = started + SKEW_S - 1
    refused("stale", lambda: peek_screen(screen(now=before), task=TASK, now=before))
    just_under = signed(SCREEN_PREFIX, SCREEN_DOMAIN, screen_claims(iat=edge - 1, exp=edge + 29))
    refused("stale", lambda: peek_screen(just_under, task=TASK, now=edge + 1))
    display = mint_display("sbx-1", now=started + 1)  # minted in this process's first seconds
    refused("stale", lambda: peek_display(display, sandbox="sbx-1", now=started + 1))


def test_a_ticket_for_another_task_or_sandbox_is_a_mismatch() -> None:
    refused("mismatch", lambda: peek_screen(screen(), task=uuid.uuid4(), now=NOW))
    refused("mismatch", lambda: peek_display(mint_display("sbx-1", now=NOW), sandbox="sbx-2", now=NOW))


def test_a_ticket_under_a_key_this_api_does_not_hold_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    ticket = screen()
    monkeypatch.setattr(settings.api, "screen_signing_key", key("k2", b"\x02" * 32))
    tickets._reset_for_tests()  # pyright: ignore[reportPrivateUsage]
    refused("unknown_kid", lambda: peek_screen(ticket, task=TASK, now=NOW))
    monkeypatch.setattr(settings.api, "screen_signing_key", key("k1", b"\x03" * 32))  # same kid, new seed
    tickets._reset_for_tests()  # pyright: ignore[reportPrivateUsage]
    refused("signature", lambda: peek_screen(ticket, task=TASK, now=NOW))


def test_dev_mints_a_key_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.api, "screen_signing_key", SecretStr(""))
    tickets._reset_for_tests()  # pyright: ignore[reportPrivateUsage]
    ticket = screen()
    assert peek_screen(ticket, task=TASK, now=NOW).kid == f"dev-{os.getpid()}"
    assert list(public_keys()) == [f"dev-{os.getpid()}"]
    tickets._reset_for_tests()  # pyright: ignore[reportPrivateUsage]  # a restart: a new key, the same kid
    refused("signature", lambda: peek_screen(ticket, task=TASK, now=NOW))


def test_spent_tickets_are_pruned_once_no_peek_could_pass() -> None:
    redeem("a" * 32, int(NOW), now=NOW)
    redeem("b" * 32, int(NOW) + 1000, now=NOW + 100)
    assert set(tickets._spent) == {"b" * 32}  # pyright: ignore[reportPrivateUsage]


def test_a_ticket_is_never_minted_that_its_peek_would_refuse() -> None:
    with pytest.raises(ValueError):
        screen(conn="short")


# ── the credential a screen ticket names ──────────────────────────────────────────────────


async def test_the_credential_dependency_names_the_row_never_the_token() -> None:
    ada = Actor(kind="human", display_name="Ada", scopes=[], status="active")
    workspace, sid, kid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    assert await credential(Principal(ada, workspace, "member", auth_session_id=sid)) == Credential(
        sid, CredKind.SESSION
    )
    assert await credential(Principal(ada, workspace, "member", api_key_id=kid)) == Credential(
        kid, CredKind.API_KEY
    )
    with pytest.raises(Forbidden):  # a person with no workspace left mints no screen ticket
        await credential(Principal(ada, None, None, auth_session_id=sid))
    with pytest.raises(Unauthorized):
        await credential(Principal(ada, workspace, "member"))


def test_the_socket_door_logs_why_a_ticket_was_refused_never_the_ticket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refusal closes 4401 whatever the reason; the reason - a skewed clock, another replica's
    key, a restart's stale window - goes on the log line, so an operator can tell it from a
    revoked credential. The ticket itself never does."""
    from prismoid.operations import screen as screen_ops

    logged: list[str] = []

    class _Log:
        def info(self, template: str, **fields: Any) -> None:
            logged.append(template.format(**fields) + " " + " ".join(str(v) for v in fields.values()))

    monkeypatch.setattr(screen_ops, "logfire", _Log())
    ticket = "st_not-a-ticket.at-all"
    assert screen_ops.admit(ticket, TASK) == screen_ops.CLOSE_UNAUTHORIZED
    assert len(logged) == 1 and "malformed" in logged[0] and str(TASK) in logged[0], logged
    assert ticket not in logged[0] and "not-a-ticket" not in logged[0]
