# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Stream tickets: what a browser opens an event stream with, since an EventSource cannot set an
Authorization header and the session token must never ride a URL. Thin: auth → one operation →
serialize. Minting needs an Actor; the ticket names the credential row this request used."""

from __future__ import annotations

from uuid import UUID

from prismoid.api.deps import ActorDep, CredentialDep, SessionDep
from prismoid.api.serializers.stream_tickets import StreamTicketOut, stream_ticket_out
from prismoid.operations import stream_tickets as ticket_ops
from prismoid.schemas.stream_tickets import StreamSubject


async def mint_task_stream_ticket(
    task_id: UUID, actor: ActorDep, cred: CredentialDep, session: SessionDep
) -> StreamTicketOut:
    """A single-use, thirty-second ticket for `GET /v1/tasks/{id}/events?ticket=…` (or the task's
    WebSocket). Anyone who can read the task may mint one; anyone else meets the task's 404."""
    ticket = await ticket_ops.mint(session, actor, cred, StreamSubject.TASK, task_id)
    return stream_ticket_out(ticket)


async def mint_conversation_stream_ticket(
    conversation_id: UUID, actor: ActorDep, cred: CredentialDep, session: SessionDep
) -> StreamTicketOut:
    """A single-use, thirty-second ticket for `GET /v1/conversations/{id}/events?ticket=…`. Only the
    conversation's owner may mint one; anyone else meets its 404."""
    ticket = await ticket_ops.mint(session, actor, cred, StreamSubject.CONVERSATION, conversation_id)
    return stream_ticket_out(ticket)
