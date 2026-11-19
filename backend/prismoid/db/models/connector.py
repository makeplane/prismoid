# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Connector - a user's connection to a remote MCP server: URL, credential, tool inventory.

The row is the whole secret store: OAuth client registration, tokens, and in-flight
flow state live here as **Fernet ciphertext** (`*_enc`, written and read only by
`operations/connectors.py` through `operations/cipher.py`) with `key_id` naming the key that
sealed them - rotation is an operational sweep, not a migration. The two plaintext exceptions
are deliberate: `oauth_state_hash` (sha256 of the single-use callback state - the lookup key
cannot be inside the box it opens) and `token_expires_at` (expiry drives refresh scheduling
and is not a secret).

Ownership is personal: `owner_actor_id` CASCADE - tokens die with the user, and only the
owner's tasks may attach the connector. Per ADR-014, workspace tenancy adds a `workspace_id`
column here when it lands; ownership stays personal until that decision is made for real.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from prismoid.db.base import Base, created_at, keyset_before, keyset_cursor, utcnow, uuid_pk, vocabulary
from prismoid.db.models import tenant
from prismoid.db.models.actor import Actor
from prismoid.errors import NotFound
from prismoid.schemas.connectors import AuthKind, ConnectorStatus


class Connector(Base):
    __tablename__ = "connectors"
    __table_args__ = (
        vocabulary("status", ConnectorStatus),
        # The slug is the tool prefix - one per owner within a workspace.
        UniqueConstraint("workspace_id", "owner_actor_id", "slug", name="uq_connectors_workspace_owner_slug"),
        Index("ix_connectors_owner_actor_id", "owner_actor_id"),
        Index("ix_connectors_created_at_id", "created_at", "id"),  # keyset pagination, only
        Index("ix_connectors_oauth_state_hash", "oauth_state_hash", unique=True),  # callback lookup
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    owner_actor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("actors.id", ondelete="CASCADE"), nullable=False
    )

    name: Mapped[str] = mapped_column(String(80), nullable=False)
    slug: Mapped[str] = mapped_column(String(48), nullable=False)
    server_url: Mapped[str] = mapped_column(String(2000), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=ConnectorStatus.PENDING_AUTH)
    auth_kind: Mapped[str] = mapped_column(String(12), nullable=False, default=AuthKind.NONE)

    # ── the sealed half (no plaintext tokens at rest) ─────────────────────────────────────────
    key_id: Mapped[str] = mapped_column(String(16), nullable=False, default="v1")
    client_info_enc: Mapped[str | None] = mapped_column(Text, default=None)  # DCR result / preregistered
    tokens_enc: Mapped[str | None] = mapped_column(Text, default=None)  # access+refresh, one blob
    flow_enc: Mapped[str | None] = mapped_column(Text, default=None)  # PKCE verifier + endpoints

    # ── the plaintext exceptions, each for a reason ───────────────────────────────────────────
    oauth_state_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    flow_expires_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    token_expires_at: Mapped[dt.datetime | None] = mapped_column(default=None)

    # ── the probed inventory (server-supplied text arrives sanitized + capped) ────────────────
    scopes: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    tools: Mapped[list[dict[str, Any]]] = mapped_column(nullable=False, default=list, server_default="[]")
    instructions: Mapped[str | None] = mapped_column(Text, default=None)
    server_info: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict, server_default="{}")
    last_error: Mapped[str | None] = mapped_column(String(500), default=None)

    # ── policy + provenance ───────────────────────────────────────────────────────────────────
    # Per-tool OVERRIDES only ({tool: allow|ask|off}); a missing key falls to the pure derived
    # default (schemas.connectors.default_tool_policy), which is what survives inventory drift.
    tool_policies: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict, server_default="{}")
    # The owner's standing grant for intent attach (default ON);
    # an auto-mount's write tools gate on approval (via `task.connectors_auto`), which is the
    # consent point - the ALLOW dial trusts an explicit Connect, not an auto-mount.
    auto_attach: Mapped[bool] = mapped_column(nullable=False, default=True, server_default="true")
    catalog_slug: Mapped[str | None] = mapped_column(String(48), default=None)  # which entry minted it

    created_at: Mapped[dt.datetime] = created_at()
    updated_at: Mapped[dt.datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # ── fetch paths ───────────────────────────────────────────────────────────────────────────

    @classmethod
    async def get_for(cls, session: AsyncSession, connector_id: uuid.UUID, actor: Actor) -> Connector:
        """The ONLY single-connector fetch path. Owner-only; `NotFound`, never Forbidden - a
        stranger asking about your connector learns nothing, not even that it exists. In the
        bound workspace only: a person's connector in one workspace is not usable from another."""
        row = await session.scalar(
            select(cls).where(
                cls.id == connector_id,
                cls.workspace_id == tenant.workspace_of(session),
                cls.owner_actor_id == actor.id,
            )
        )
        if row is None:
            raise NotFound("no such connector")
        return row

    @classmethod
    async def for_owner(
        cls, session: AsyncSession, actor_id: uuid.UUID, *, limit: int = 50, cursor: str | None = None
    ) -> list[Connector]:
        query = (
            select(cls)
            .where(cls.workspace_id == tenant.workspace_of(session), cls.owner_actor_id == actor_id)
            .order_by(cls.created_at.desc(), cls.id.desc())
            .limit(limit)
        )
        if cursor is not None:
            query = query.where(keyset_before(cls, cursor))
        return list(await session.scalars(query))

    @classmethod
    async def resolve_refs(
        cls, session: AsyncSession, actor_id: uuid.UUID, refs: list[str]
    ) -> dict[str, Connector]:
        """`refs` are what users type: ids or slugs, mixed. Returns `{ref: row}` for the ones that
        resolved *within this owner's connectors* - the caller decides whether a miss is a 422
        (the task door) or a skip (a fork inheriting a since-deleted connector)."""
        ids: list[uuid.UUID] = []
        slugs: list[str] = []
        for ref in refs:
            try:
                ids.append(uuid.UUID(ref))
            except ValueError:
                slugs.append(ref)
        query = select(cls).where(
            cls.workspace_id == tenant.workspace_of(session),
            cls.owner_actor_id == actor_id,
            (cls.id.in_(ids) | cls.slug.in_(slugs))
            if ids and slugs
            else cls.id.in_(ids)
            if ids
            else cls.slug.in_(slugs),
        )
        rows = list(await session.scalars(query))
        by_id = {str(r.id): r for r in rows}
        by_slug = {r.slug: r for r in rows}
        return {ref: row for ref in refs if (row := by_id.get(ref) or by_slug.get(ref)) is not None}

    @classmethod
    async def for_ids(
        cls, session: AsyncSession, ids: list[uuid.UUID], *, owner_id: uuid.UUID
    ) -> list[Connector]:
        """The step-time load: rows for a task's stored `connector_ids`, order preserved.
        Owner-scoped like every other fetch path - a task mounts only its creator's
        connectors, so a foreign id (however it landed on a row) loads nothing rather than
        somebody else's credential. The parameter is keyword-only so no caller can pass an
        id list without saying whose connectors it expects."""
        if not ids:
            return []
        rows = {
            r.id: r
            for r in await session.scalars(
                select(cls).where(
                    cls.id.in_(ids),
                    cls.workspace_id
                    == tenant.workspace_of(session),  # a task mounts its own workspace's rows only
                    cls.owner_actor_id == owner_id,
                )
            )
        }
        return [rows[i] for i in ids if i in rows]

    @classmethod
    async def consume_state(cls, session: AsyncSession, state_hash: str) -> Connector | None:
        """The callback's single-use claim, atomically: match the hash **and** the TTL, clear the
        hash in the same statement. A replayed state - or one arriving after `flow_expires_at` -
        matches zero rows, and the caller renders the honest failure page. UPDATE…RETURNING so
        two racing callbacks cannot both win."""
        claimed = (
            await session.execute(
                update(cls)
                .where(cls.oauth_state_hash == state_hash, cls.flow_expires_at > utcnow())
                .values(oauth_state_hash=None)
                .returning(cls.id)
            )
        ).scalar_one_or_none()
        if claimed is None:
            return None
        return await session.get(cls, claimed)

    def cursor(self) -> str:
        return keyset_cursor(self)

    def tool_names(self) -> tuple[str, ...]:
        return tuple(str(t.get("name", "")) for t in self.tools if t.get("name"))
