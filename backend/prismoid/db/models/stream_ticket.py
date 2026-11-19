# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""StreamTicket - a browser's single-use, seconds-long admission to one event stream.

An EventSource cannot set an Authorization header, so whatever authenticates it rides the URL, and
a URL lands in access logs, proxies and browser history. The ticket is what rides it instead of the
session token: 32 random bytes shown once, stored only as a SHA-256 (`hash_token`), bound to the
credential ROW that minted it (an `auth_sessions` or `api_keys` id, never the token), to that
credential's actor, and to one subject (a task or a conversation).

Redemption is one DELETE ... RETURNING: the statement that finds a live ticket also removes it, so
two racing streams cannot both open on one ticket, on one replica or several. Postgres `now()` is
the clock for both the mint and the check, so API hosts with drifting clocks agree. A ticket nobody
redeemed is swept by a later mint once it has expired (`sweep_expired`).
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import ForeignKey, String, delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from prismoid.db.base import Base, StrEnumColumn, created_at, uuid_pk, vocabulary
from prismoid.schemas.ids import hash_token, new_stream_ticket
from prismoid.schemas.principal import Credential, CredKind
from prismoid.schemas.stream_tickets import StreamSubject

# Expired tickets one mint removes at most: enough to keep pace with the mints that leave them,
# small enough that no mint waits on a long delete.
_SWEEP_BATCH = 100


@dataclass(frozen=True)
class TicketClaim:
    """What a redeemed ticket said: who minted it, with which credential row, for which stream."""

    actor_id: uuid.UUID
    credential: Credential
    subject: StreamSubject
    subject_id: uuid.UUID


class StreamTicket(Base):
    __tablename__ = "stream_tickets"
    __table_args__ = (
        vocabulary("subject_kind", StreamSubject),
        vocabulary("credential_kind", CredKind),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    actor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("actors.id", ondelete="CASCADE"), nullable=False)
    # The credential row the minting request authenticated with. No foreign key: it names one of two
    # tables, and a revoked or deleted credential already fails the proof the redeem runs.
    credential_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    credential_kind: Mapped[CredKind] = mapped_column(StrEnumColumn(CredKind, 16), nullable=False)
    subject_kind: Mapped[StreamSubject] = mapped_column(StrEnumColumn(StreamSubject, 16), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    expires_at: Mapped[dt.datetime] = mapped_column(nullable=False, index=True)
    created_at: Mapped[dt.datetime] = created_at()

    @classmethod
    async def mint(
        cls,
        session: AsyncSession,
        *,
        actor_id: uuid.UUID,
        credential: Credential,
        subject: StreamSubject,
        subject_id: uuid.UUID,
        ttl: dt.timedelta,
    ) -> str:
        """Store a new ticket's hash and return the plaintext, which exists only in the response."""
        raw = new_stream_ticket()
        await session.execute(
            insert(cls).values(
                id=uuid.uuid4(),
                token_hash=hash_token(raw),
                actor_id=actor_id,
                credential_id=credential.id,
                credential_kind=credential.kind,
                subject_kind=subject,
                subject_id=subject_id,
                expires_at=func.now() + ttl,
            )
        )
        return raw

    @classmethod
    async def redeem(cls, session: AsyncSession, raw: str) -> TicketClaim | None:
        """Spend a live ticket and say what it was for, or None: unknown, expired or already spent
        all read the same, so the caller's refusal cannot narrate which."""
        row = (
            await session.execute(
                delete(cls)
                .where(cls.token_hash == hash_token(raw), cls.expires_at > func.now())
                .returning(
                    cls.actor_id, cls.credential_id, cls.credential_kind, cls.subject_kind, cls.subject_id
                )
            )
        ).one_or_none()
        if row is None:
            return None
        actor_id, credential_id, credential_kind, subject_kind, subject_id = row
        return TicketClaim(
            actor_id=actor_id,
            credential=Credential(id=credential_id, kind=credential_kind),
            subject=subject_kind,
            subject_id=subject_id,
        )

    @classmethod
    async def sweep_expired(cls, session: AsyncSession) -> None:
        """Delete a batch of tickets that expired unredeemed. Rows another mint is already deleting
        are skipped, so concurrent mints never queue behind each other here."""
        expired = (
            select(cls.id)
            .where(cls.expires_at <= func.now())
            .limit(_SWEEP_BATCH)
            .with_for_update(skip_locked=True)
        )
        await session.execute(delete(cls).where(cls.id.in_(expired.scalar_subquery())))
