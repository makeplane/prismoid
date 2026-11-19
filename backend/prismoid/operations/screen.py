# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The screen's door (ADR-065): the control routes, the socket's ticket, and the checks and verbs
the screen socket (`api/screen_socket.py`) runs on this process's screen relay (`relay`, a `Relay`:
`operations/screen_relay.py` and the two layers it is built on, `screen_fanout.py` and
`screen_control.py`).

**The socket and its ticket**: `mint_ticket` hands anyone who can read the task a single-use `st_`
ticket. The socket accepts first and then asks this module, in order: the ticket (`admit`: peeked,
the switch, redeemed), the door (`prove`), and a holder who may no longer edit loses the lease
(`settle_role`). From then on it feeds this process's relay: the browser's binary messages up
(`on_message`; `up_message` refuses anything malformed with the generic policy close, 1008), the
task's `screen.state` and `machine.*` events from the hub (`on_event`), and a re-proof every
`stream_reauth_seconds` (`reprove`). The three control routes go through `claim_control`,
`extend_control` and `release_control`, which re-read the lease on this process's relay before
the route answers, so a private claim masks here without waiting for `screen.state` to come back
round the hub. Neither the ticket nor its claims ever reach a log line.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

import logfire
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.conf import settings
from prismoid.db.base import utcnow
from prismoid.db.models import tenant
from prismoid.db.models.actor import Actor
from prismoid.db.models.api_key import ApiKey
from prismoid.db.models.machine import Machine
from prismoid.db.models.session import AuthSession
from prismoid.db.models.task import Task
from prismoid.db.session import session_scope
from prismoid.errors import Conflict, NotFound
from prismoid.operations import steering, tickets
from prismoid.operations.machines import screen as machine_screen
from prismoid.operations.screen_relay import Relay
from prismoid.operations.screen_state import Viewer
from prismoid.operations.tickets import ScreenClaims, TicketRefused
from prismoid.schemas.principal import CredKind, Principal
from prismoid.schemas.screen import (
    CONTROL_TYPES,
    INPUT_TYPES,
    MESSAGE_MAX,
    UP_TYPES,
    ControlEnd,
    LeaseView,
    WireError,
    decode,
    is_live,
)

relay = Relay()


# ── the control routes, on this process's relay (the private-switch window) ───────────────────
# The functions below read `relay` as a module global at call time, so a test's
# `monkeypatch.setattr(screen_ops, "relay", Relay(...))` reaches them.


async def claim_control(
    session: AsyncSession, actor: Actor, task_id: uuid.UUID, *, conn: str, private: bool
) -> LeaseView:
    """`POST /v1/tasks/{id}/screen/control`: the claim (`machines.screen.claim`), then this
    process's relay re-reads the lease before the route answers. `screen.state` reaches every
    process through the hub a few milliseconds later; this closes that window here, so when the
    holder's "Private" comes back, every other screen this process serves is already masked."""
    view = await machine_screen.claim(session, actor, task_id, conn=conn, private=private)
    await _reread_here(task_id)
    return view


async def extend_control(session: AsyncSession, actor: Actor, task_id: uuid.UUID) -> LeaseView:
    """`POST /v1/tasks/{id}/screen/control/renew`: `machines.screen.extend`, then the same re-read."""
    view = await machine_screen.extend(session, actor, task_id)
    await _reread_here(task_id)
    return view


async def release_control(session: AsyncSession, actor: Actor, task_id: uuid.UUID) -> None:
    """`DELETE /v1/tasks/{id}/screen/control`: `machines.screen.release`, then the same re-read."""
    await machine_screen.release(session, actor, task_id)
    await _reread_here(task_id)


async def _reread_here(task_id: uuid.UUID) -> None:
    """This process's relay re-reads the task's lease (a no-op when it relays no screen of it). The
    operation has committed. The relay finds the Machine itself, so nothing here can fail open: a
    lease read that fails leaves the relay blind, and the mask closed, until a read succeeds."""
    await relay.task_lease_changed(task_id)


# ── the socket and its ticket ─────────────────────────────────────────────────────────────────

CLOSE_UNAUTHORIZED: Final = 4401  # a bad ticket, or a failed proof
CLOSE_NOT_FOUND: Final = 4404  # no such task or Machine; a reset
CLOSE_OFF: Final = 4410  # computer use is off
CLOSE_POLICY: Final = 1008  # a malformed message: the generic policy close, never 4401
CLOSE_AGAIN: Final = 1013  # the hub dropped this socket as a slow subscriber: reconnect
CLOSE_ERROR: Final = 1011
_REASONS: Final = {
    CLOSE_UNAUTHORIZED: "unauthorized",
    CLOSE_NOT_FOUND: "not found",
    CLOSE_OFF: "computer use is off",
}

Closing = tuple[int, str]


@dataclass(frozen=True, slots=True)
class Standing:
    """What a passing door or proof found: the task's Machine, and whether the actor may edit."""

    machine_id: uuid.UUID
    editor: bool


async def mint_ticket(
    session: AsyncSession,
    actor: Actor,
    task_id: uuid.UUID,
    *,
    cred_id: uuid.UUID,
    cred_kind: CredKind,
    conn: str,
) -> str:
    """`POST /v1/tasks/{id}/screen/ticket`: anyone who can read the task. The ticket
    names the credential row this request authenticated with (never the token), the actor and the
    client's connection; single-use, `ticket_ttl_s` long. The task is fetched first, so a stranger
    meets the same 404 whatever the switch says; with computer use off it is the one "off" 409."""
    task = await Task.get_for(session, task_id, actor)
    if not settings.computer_use.enabled:
        raise Conflict(machine_screen.OFF)
    return tickets.mint_screen(task=task.id, actor=actor.id, cred=cred_id, cred_kind=cred_kind, conn=conn)


def up_message(message: Mapping[str, Any]) -> tuple[dict[str, Any], bytes]:
    """One message from the browser as the relay may see it, or `WireError`, which the socket
    answers with the policy close, 1008: binary only; at most MESSAGE_MAX; a JSON
    header within HEADER_MAX whose `n` is its payload's length (`decode`); a kind the browser may
    send (`UP_TYPES`); a payload on `vnc` alone (the holder's RFB, `CONTROL_TYPES`), and never an
    empty one there."""
    data = message.get("bytes")
    if not isinstance(data, bytes):
        raise WireError("the screen speaks binary: a text frame is refused")
    if len(data) > MESSAGE_MAX:
        raise WireError("message too large")
    header, payload = decode(data)
    kind = header.get("t")
    if kind not in UP_TYPES or (kind in CONTROL_TYPES) != bool(payload):
        raise WireError("not a message the screen accepts")
    if kind in CONTROL_TYPES and set(header) - {"t", "n"}:
        raise WireError("not a message the screen accepts")
    return header, payload


def up_header(message: Mapping[str, Any]) -> dict[str, Any]:
    """`up_message`'s header (its callers that need no payload)."""
    return up_message(message)[0]


def admit(ticket: str, task_id: uuid.UUID) -> ScreenClaims | int:
    """The ticket's half of the door, or its close code: peeked (4401), the switch (4410), then
    redeemed with the claims the peek returned (4401: another socket spent it first). Peek and
    redeem run with no await between them, on this event loop."""
    try:
        claims = tickets.peek_screen(ticket, task=task_id)
        if not settings.computer_use.enabled:
            return CLOSE_OFF
        tickets.redeem(claims.jti, claims.exp)
    except TicketRefused as refused:
        return _refused(CLOSE_UNAUTHORIZED, refused.reason, task_id)
    return claims


async def on_message(viewer: Viewer, message: Mapping[str, Any]) -> Closing | None:
    """One message from the browser, to this process's relay: a hint, an input, or the holder's
    RFB. Anything malformed is the generic policy close."""
    try:
        header, payload = up_message(message)
    except WireError:
        return (CLOSE_POLICY, "malformed")
    if header["t"] == "hint":
        await relay.hint(viewer, header)
    elif header["t"] in INPUT_TYPES:
        await relay.input(viewer, header)
    elif header["t"] in CONTROL_TYPES:
        await relay.control_input(viewer, payload)
    return None


async def on_event(viewer: Viewer, item: Any) -> Closing | None:
    """The task's feed. A reset ends every screen on the Machine at once (4404: the old incarnation
    is gone). Any other `machine.*` is the Machine's life moving (created, woke,
    rebuilt, slept, a lease claimed or ended): `wake` nudges an upstream that is down and re-reads
    the lease. `screen.state` re-reads the lease only - it never cuts a backoff
    short. Everything else on the task's stream is not the screen's."""
    kind = getattr(item, "type", None)
    if not isinstance(kind, str):
        return (CLOSE_AGAIN, "lagging")  # the hub's Poison: it dropped this socket as too slow
    if kind == "machine.reset":
        await relay.close_machine(viewer.machine_id, CLOSE_NOT_FOUND, "reset")
    elif kind.startswith("machine."):
        await relay.wake(viewer.machine_id)
    elif kind == "screen.state":
        await relay.lease_changed(viewer.machine_id)
    return None


async def reprove(viewer: Viewer, claims: ScreenClaims) -> Closing | None:
    """Every `stream_reauth_seconds`: the switch first - off, the lease is re-read and
    a holder's ends `disabled`, then the socket closes 4410 - then the door again
    (4401 or 4404). A holder who can no longer see the task (removed from its Project, the task
    deleted) loses the lease (`demoted`) before the 4404, so nobody else stays masked through the
    grace behind someone who cannot see the screen. On a pass the lease is re-read (the relay's
    backstop) and the role re-checked."""
    if not settings.computer_use.enabled:
        await relay.lease_changed(viewer.machine_id)
        await _end_if_held(viewer, ControlEnd.DISABLED)
        return refusal(CLOSE_OFF)
    standing = await prove(claims)
    if isinstance(standing, int):
        if standing == CLOSE_NOT_FOUND:
            await relay.lease_changed(viewer.machine_id)
            await _end_if_held(viewer, ControlEnd.DEMOTED)
        return refusal(standing)
    if standing.machine_id != viewer.machine_id:
        return refusal(CLOSE_NOT_FOUND)
    await relay.lease_changed(viewer.machine_id)
    await settle_role(viewer, standing)
    return None


async def settle_role(viewer: Viewer, standing: Standing) -> None:
    """A holder who is no longer an editor loses the lease (`demoted`) and stays as a
    viewer."""
    if not standing.editor:
        await _end_if_held(viewer, ControlEnd.DEMOTED)


async def _end_if_held(viewer: Viewer, reason: ControlEnd) -> None:
    """End this actor's lease (`end_live`) when the cached lease - expiry computed on read - is
    theirs: only a holder's own sockets take the row's lock, and the row's compare-and-set decides.
    A failure is logged; the next proof asks again (a switch-off's last proof leaves it to the
    lease's own clock)."""
    held = relay.held(viewer.machine_id)
    if held is None or held.lease.actor_id != viewer.actor_id or not is_live(held.lease, utcnow()):
        return
    try:
        await machine_screen.end_live(viewer.machine_id, viewer.actor_id, reason=reason)
    except Exception:
        logfire.exception(
            "screen socket: the lease did not end ({reason})",
            reason=str(reason),
            machine_id=str(viewer.machine_id),
        )


async def prove(claims: ScreenClaims) -> Standing | int:
    """The door, by credential id: the row the ticket names is still live and
    still the ticket's actor's, the account still stands, the task is still visible to it, the workspace
    fence holds, and the task still has its Machine. The close code on failure; fail-CLOSED on an
    error, as the task stream's re-proof is (`api/stream._still_authorized_for`)."""
    try:
        async with session_scope() as session:
            principal = await _principal(session, claims)
            if principal is None:
                return _refused(CLOSE_UNAUTHORIZED, "credential", claims.task)
            try:
                task = await Task.get_for(session, claims.task, principal.actor)
            except NotFound:
                return _refused(CLOSE_NOT_FOUND, "task", claims.task)
            if task.workspace_id != principal.workspace_id:
                return _refused(CLOSE_UNAUTHORIZED, "workspace", claims.task)
            row = await Machine.for_task(session, task.id)
            if row is None:
                return _refused(CLOSE_NOT_FOUND, "machine", claims.task)
            editor = await steering.may_edit(session, task, principal.actor)
            return Standing(machine_id=row.id, editor=editor)
    except Exception:
        logfire.warning("screen re-proof errored - severing", task_id=str(claims.task))
        return CLOSE_UNAUTHORIZED


async def _principal(session: AsyncSession, claims: ScreenClaims) -> Principal[Actor] | None:
    """`deps.authenticate`'s checks for a credential named by id: the row is live
    (`live_principal`), the actor is still the ticket's and still active, and a workspace is left to act
    in - which binds the session, as the door binds a request."""
    model = AuthSession if claims.cred_kind == CredKind.SESSION else ApiKey
    principal = await model.live_principal(session, claims.cred)
    if principal is None or principal.actor.id != claims.actor or not principal.actor.is_active():
        return None
    if principal.workspace_id is None:
        return None
    tenant.bind(session, principal.workspace_id, principal.role)
    return principal


def _refused(code: int, why: str, task_id: uuid.UUID) -> int:
    """A door's refusal, with why on the log line: a skewed clock, another replica's key or a
    restart's stale window must not read like a revoked credential to an operator. The reason and
    the task only - never the ticket or its claims."""
    logfire.info("screen socket refused ({why})", why=why, code=code, task_id=str(task_id))
    return code


def refusal(code: int) -> Closing:
    return (code, _REASONS[code])
