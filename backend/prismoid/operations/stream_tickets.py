# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Stream tickets: how a browser opens a task's or a conversation's event stream without putting
its session token in the URL.

An EventSource cannot set an Authorization header, so the browser first asks for a ticket with an
ordinary authenticated POST (`mint`), then opens `/events?ticket=et_…`. The ticket is single-use and
lives thirty seconds; it names the credential ROW the POST authenticated with, never the token, and
one subject. The stream door spends it (`admit`) and proves that credential row live again by id
(`prove`), the same proof the open stream repeats every `stream_reauth_seconds`: a revoked session
or key, a suspended account or a lost membership severs a ticketed stream exactly as it severs a
header-authenticated one.

Every refusal reads the same ("this stream ticket is invalid or has expired"): unknown, expired,
spent, minted for another subject or by a credential that no longer proves. A ticket that reached
the door is spent even when it is refused, so a ticket presented at the wrong stream is gone.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models import tenant
from prismoid.db.models.actor import Actor
from prismoid.db.models.api_key import ApiKey
from prismoid.db.models.conversation import Conversation
from prismoid.db.models.session import AuthSession
from prismoid.db.models.stream_ticket import StreamTicket
from prismoid.db.models.task import Task
from prismoid.errors import Unauthorized
from prismoid.schemas.ids import STREAM_TICKET_PREFIX
from prismoid.schemas.principal import Credential, CredKind, Principal
from prismoid.schemas.stream_tickets import STREAM_TICKET_TTL_S, StreamSubject

_DEAD = "this stream ticket is invalid or has expired"


async def mint(
    session: AsyncSession,
    actor: Actor,
    credential: Credential,
    subject: StreamSubject,
    subject_id: uuid.UUID,
) -> str:
    """A ticket for one stream the actor can read right now. The subject is fetched through its
    one fetch path first, so a stranger meets the subject's own 404 and no ticket. Commits the
    ticket row, and sweeps a batch of expired ones in the same transaction."""
    if subject is StreamSubject.TASK:
        await Task.get_for(session, subject_id, actor)
    else:
        await Conversation.get_for(session, subject_id, actor)
    await StreamTicket.sweep_expired(session)
    raw = await StreamTicket.mint(
        session,
        actor_id=actor.id,
        credential=credential,
        subject=subject,
        subject_id=subject_id,
        ttl=dt.timedelta(seconds=STREAM_TICKET_TTL_S),
    )
    await session.commit()
    return raw


async def admit(
    session: AsyncSession, raw: str, subject: StreamSubject, subject_id: uuid.UUID
) -> Principal[Actor]:
    """Spend the ticket and return the principal its credential row proves, with the session bound
    to that principal's workspace as the door binds a request. Commits the spend before it checks
    anything else, so a refused ticket is spent too. `Unauthorized` for every refusal."""
    if not raw.startswith(STREAM_TICKET_PREFIX):
        raise Unauthorized(_DEAD)  # not a ticket at all: refused without a database read
    claim = await StreamTicket.redeem(session, raw)
    await session.commit()
    if claim is None or claim.subject != subject or claim.subject_id != subject_id:
        raise Unauthorized(_DEAD)
    principal = await prove(session, claim.credential, claim.actor_id)
    if principal is None:
        raise Unauthorized(_DEAD)
    return principal


async def prove(
    session: AsyncSession, credential: Credential, actor_id: uuid.UUID
) -> Principal[Actor] | None:
    """The door's checks for a credential named by row id: the row is live, it still belongs to
    `actor_id`, the account is active, and a workspace is left to act in. Binds the session to that
    workspace (and pins it for an API key, which never acts elsewhere); None when any check fails.
    Read-only: it never rebinds a session or bumps a key's last use."""
    model = AuthSession if credential.kind == CredKind.SESSION else ApiKey
    principal = await model.live_principal(session, credential.id)
    if principal is None or principal.actor.id != actor_id or not principal.actor.is_active():
        return None
    if principal.workspace_id is None:
        return None
    tenant.bind(session, principal.workspace_id, principal.role)
    if credential.kind == CredKind.API_KEY:
        tenant.pin(session, principal.workspace_id)
    return principal
