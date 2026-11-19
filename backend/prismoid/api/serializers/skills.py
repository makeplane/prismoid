# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Skills - response bodies. `*Out` = response.

A skill row carries its routing surface in the `manifest` JSONB; the API lifts the two fields a
human reading `prismoid skills list` actually wants - the description they route on and the
capability it hints at - up to the top level, so a caller never has to reach into a nested blob to
learn what a skill is for. The body itself is not here: it lives in MinIO by hash, and listing the
menu must never cost twelve object fetches.
"""

from __future__ import annotations

import datetime as dt
import math
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from prismoid.conf import settings
from prismoid.db.models.skill import SkillRecord
from prismoid.schemas.ids import SkillName
from prismoid.schemas.skills import MAX_SKILL_BUNDLE_BYTES, MAX_SKILL_FILES

# One file can be the whole bundle; base64 grows it by a third.
_MAX_FILE_BASE64_CHARS = 4 * math.ceil(MAX_SKILL_BUNDLE_BYTES / 3)


class SkillFileIn(BaseModel):
    """One bundled file, base64 because a script or template may not be UTF-8. The
    path law (scripts/ references/ assets/, no traversal) is enforced server-side in
    `format.parse` - the one door - never trusted from the client."""

    path: str = Field(min_length=1, max_length=200)
    content_b64: str = Field(max_length=_MAX_FILE_BASE64_CHARS)


class SkillPushIn(BaseModel):
    """An author pushing an edited skill - the *raw* `SKILL.md`, `cases.yaml`, and any bundled
    files, validated server-side. The client never sends a parsed manifest: the write-time
    doctrine is enforced in one place (`format.parse`), so a draft that skipped the CLI and hit
    the API directly obeys the same rules."""

    skill_md: str = Field(min_length=1, max_length=settings.api.max_text_chars)
    cases_yaml: str = Field(default="", max_length=settings.api.max_text_chars)
    files: list[SkillFileIn] = Field(default_factory=list, max_length=MAX_SKILL_FILES)


class SkillCreateIn(SkillPushIn):
    """Creating a brand-new skill (POST) carries its name in the body; a push to an existing one (PUT)
    takes the name from the path."""

    name: SkillName


class SkillOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    version: int
    status: str  # draft | published | archived
    origin: str  # builtin | user
    description: str  # the routing trigger a router reads
    capability: str  # the ModelClass this skill's work tends to want
    eval_status: str  # untested | green | red - publishing needs green
    body_sha256: str
    created_by: UUID | None
    created_at: dt.datetime


def skill_out(record: SkillRecord) -> SkillOut:
    manifest = record.manifest or {}
    return SkillOut(
        name=record.name,
        version=record.version,
        status=record.status,
        origin=record.origin,
        description=str(manifest.get("description", "")),
        capability=str(manifest.get("capability", "")),
        eval_status=record.eval_status,
        body_sha256=record.body_sha256,
        created_by=record.created_by,
        created_at=record.created_at,
    )
