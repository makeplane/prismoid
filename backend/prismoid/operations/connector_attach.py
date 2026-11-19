# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Intent attach (ADR-054): the owner's live connectors whose vocabulary a goal speaks.

The matcher is `schemas.connector_catalog.suggest` - pure, word-boundary, no model - and the
vocabulary recipe is `schemas.connector_catalog.vocabulary`, so the composer's chip, this door and
the action door can never disagree. This module is the thin row adapter over both.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models.connector import Connector
from prismoid.schemas.connectors import ConnectorStatus


def service_words(row: Connector) -> set[str]:
    """One connector's suggestion vocabulary - a thin row adapter over the ONE pure
    recipe `schemas.connector_catalog.vocabulary`, which the serializer and every matcher share
    so the composer chip, auto-attach, and the action door can never disagree."""
    from prismoid.schemas.connector_catalog import vocabulary

    return vocabulary(
        slug=row.slug, name=row.name, catalog_slug=row.catalog_slug, tool_names=tuple(row.tool_names())
    )


async def auto_attach_candidates(
    session: AsyncSession, owner_id: uuid.UUID, goal: str
) -> tuple[list[str], list[str]]:
    """The owner's LIVE, `auto_attach`-enabled connectors whose vocabulary the goal speaks.
    Returns (row ids, "slug:keyword" provenance), both in match order, deterministically.

    A candidate's vocabulary = its catalog entry's keywords (via `catalog_slug`) + its own
    slug + its name's words + its probed TOOL-NAME tokens split on `_` - the tool tokens are
    what make "email this to me" attach a custom connector exposing `send_email`, catalog-
    listed or not. The matcher is `schemas.connector_catalog.suggest`: pure, word-boundary,
    no model - the same grammar the composer's chip runs client-side, so what the user saw
    offered is what this door would attach. The triage doorway law holds because the GRANT is
    the owner's standing `auto_attach` flag, and the attach lands on the record
    (`TaskCreated.connectors_auto`).
    """
    from prismoid.schemas.connector_catalog import suggest

    rows = await Connector.for_owner(session, owner_id, limit=200)
    candidates: list[tuple[str, tuple[str, ...]]] = []
    by_id: dict[str, Connector] = {}
    for row in rows:
        if row.status != ConnectorStatus.LIVE or not row.auto_attach:
            continue
        by_id[str(row.id)] = row
        candidates.append((str(row.id), tuple(sorted(service_words(row)))))

    hits = suggest(goal, candidates)
    ids = [cid for cid, _ in hits]
    provenance = [f"{by_id[cid].slug}:{kw}" for cid, kw in hits]
    return ids, provenance
