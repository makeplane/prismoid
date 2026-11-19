# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The manifest - the provenance chain that makes an artifact traceable.

`frame.publish()` appends one `ManifestEntry` per published file. Harvest reads the
manifest, uploads content-addressed, and inserts the `Artifact` rows with the entry as
provenance. Nothing outside `/artifacts` + this manifest is harvestable - which is the
whole of Rule 9, expressed as a file.
"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, ConfigDict, Field

from prismoid.schemas.contract import SkillPin
from prismoid.schemas.ids import ArtifactId, TaskId
from prismoid.schemas.outputs import ArtifactType, RenderHint
from prismoid.schemas.verification import Verdict


class ManifestEntry(BaseModel):
    """One published file. `sources` is what it was derived from - the verifier re-derives from
    exactly these, so an entry that lies about its sources fails its own verification."""

    model_config = ConfigDict(frozen=True)

    path: str  # relative to /artifacts
    type: ArtifactType
    title: str
    sha256: str
    bytes: int
    sources: list[str] = Field(default_factory=list)  # frame paths and/or URLs

    agent: str
    model: str
    skill_pins: list[SkillPin] = Field(default_factory=list[SkillPin])
    at: dt.datetime

    # The normalizer's honest ledger: what harvest repaired to make the published
    # bytes valid (collections today). Empty = stored exactly as published. When non-empty,
    # `sha256`/`bytes` describe the STORED (repaired) bytes - the hash a reader can check.
    repairs: list[str] = Field(default_factory=list)


class Manifest(BaseModel):
    """`/artifacts/manifest.json`. Publishing the same path twice appends a new version."""

    model_config = ConfigDict(frozen=True)

    entries: list[ManifestEntry] = Field(default_factory=list[ManifestEntry])

    def latest(self) -> dict[str, ManifestEntry]:
        """Last write wins, per path - the manifest is a log, and this is its head."""
        out: dict[str, ManifestEntry] = {}
        for e in self.entries:
            out[e.path] = e
        return out


class ArtifactView(BaseModel):
    """An artifact as the API returns it. Never an ORM row."""

    model_config = ConfigDict(frozen=True)

    id: ArtifactId
    task_id: TaskId  # the owning task - an artifact is only ever reached through it
    type: ArtifactType
    title: str
    filename: str  # the published file's basename - the extension a client renders by
    render: RenderHint  # the registry's answer to "how do I present these bytes?"
    lineage_id: ArtifactId  # the revision chain's identity (root row's id)
    revision: int  # 1 = as published; each save mints the next
    label: str | None = None  # the user's name for THIS revision ("final draft")
    has_preview: bool = False  # a render sidecar exists (GET /preview)
    has_rendition: bool = False  # a full-fidelity rendition exists (GET /rendition)
    rendition_media: str | None = None  # "application/pdf" | "text/html" - how a client shows it
    sha256: str
    bytes: int
    role: str = "deliverable"  # deliverable | working - the shelf's lens key
    provenance: ManifestEntry
    verdict: Verdict | None = None  # None ⇒ not verified yet; `unverified` is an explicit mark
    created_at: dt.datetime
