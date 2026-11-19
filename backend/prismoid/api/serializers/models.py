# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Models - the catalog's read shape. `*Out` = response.

Moved out of the admin serializer when the catalog grew a non-admin read (the fleet seat
picker needs to know what is flyable); the admin surface imports the same shape, so the wire
stays byte-identical.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from prismoid.db.models.model_catalog import ModelCatalogEntry


class ModelOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    ref: str  # provider:model_id
    provider: str
    model_id: str
    family: str
    protocol: str
    classes: list[str]
    context_window: int
    base_url: str | None
    enabled: bool


def model_out(entry: ModelCatalogEntry) -> ModelOut:
    return ModelOut(
        ref=f"{entry.provider}:{entry.model_id}",
        provider=entry.provider,
        model_id=entry.model_id,
        family=entry.family,
        protocol=entry.protocol,
        classes=list(entry.classes),
        context_window=entry.context_window,
        base_url=entry.base_url,
        enabled=entry.enabled,
    )
