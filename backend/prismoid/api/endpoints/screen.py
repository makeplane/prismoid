# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The screen: the control lease, the socket's ticket and the socket. Thin:
auth → validate → one operation → serialize; the lease's transactions are the operation's,
and the socket's whole life is `api/screen_socket.serve_socket`'s."""

from __future__ import annotations

from uuid import UUID

from fastapi import WebSocket

from prismoid.api.deps import ActorDep, CredentialDep, SessionDep
from prismoid.api.hub import hub
from prismoid.api.screen_socket import serve_socket
from prismoid.api.serializers.screen import (
    ScreenControlIn,
    ScreenControlOut,
    ScreenTicketIn,
    ScreenTicketOut,
    control_out,
    ticket_out,
)
from prismoid.operations import screen as screen_ops


async def claim_screen_control(
    task_id: UUID, body: ScreenControlIn, actor: ActorDep, session: SessionDep
) -> ScreenControlOut:
    view = await screen_ops.claim_control(session, actor, task_id, conn=body.conn, private=body.private)
    return control_out(view)


async def renew_screen_control(task_id: UUID, actor: ActorDep, session: SessionDep) -> ScreenControlOut:
    return control_out(await screen_ops.extend_control(session, actor, task_id))


async def release_screen_control(task_id: UUID, actor: ActorDep, session: SessionDep) -> None:
    await screen_ops.release_control(session, actor, task_id)


async def mint_screen_ticket(
    task_id: UUID, body: ScreenTicketIn, actor: ActorDep, cred: CredentialDep, session: SessionDep
) -> ScreenTicketOut:
    ticket = await screen_ops.mint_ticket(
        session, actor, task_id, cred_id=cred.id, cred_kind=cred.kind, conn=body.conn
    )
    return ticket_out(ticket)


async def stream_screen(websocket: WebSocket, task_id: UUID) -> None:
    """`GET /v1/tasks/{id}/screen?ticket=st_…[&pane=1]`: the ticket is the whole admission - no
    Actor dependency, because a browser cannot set a socket's headers and no long-lived credential
    may ride a URL. The ticket is read here, never declared as a parameter, so it is no endpoint
    argument a span could record. The hub is handed in."""
    await serve_socket(websocket, task_id, websocket.query_params.get("ticket", ""), feed=hub)
