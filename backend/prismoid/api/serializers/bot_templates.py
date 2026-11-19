# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Bot templates - request bodies and responses (ADR-056). `*In` = request, `*Out` = response.

`TemplatePreviewOut` is the ANONYMOUS surface: it lifts the manifest flat (the `bot_out`
pattern) and deliberately omits `eval_report` (it embeds the author's roster - other bots'
names), the author's email, actor ids, and project names. Everything it DOES carry installs
verbatim - the transparency contract.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from prismoid.db.models.bot_template import BotTemplate
from prismoid.db.models.bot_template_install import BotTemplateInstall


class TemplatePublishIn(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1, max_length=64)
    project_id: UUID | None = None
    author_name: str = Field(min_length=1, max_length=120)  # the byline, confirmed by the author
    acknowledge_urls: bool = False


class TemplateInstallIn(BaseModel):
    model_config = ConfigDict(frozen=True)

    rename: str | None = Field(default=None, min_length=1, max_length=64)


class TemplatePreviewOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    slug: str
    template_version: int
    name: str
    title: str | None
    author_name: str  # a display-name snapshot, unverified, and the strip says so
    description: str
    job: str
    use_when: list[str]
    not_for: list[str]
    capability: str
    produces: str
    tools: list[str]
    skills: list[str]
    connectors: list[str]
    connector_why: dict[str, str]
    can_ask_user: bool
    instructions: str  # FULL - a stranger reads exactly the judgment they are installing
    cases: list[dict[str, str]]
    icon: str | None
    icon_hue: int | None
    install_count: int
    created_at: dt.datetime


def preview_out(t: BotTemplate) -> TemplatePreviewOut:
    manifest: dict[str, Any] = t.manifest or {}
    # The instructions live inside the snapshot bytes; lift them without re-validating (the
    # door validated at publish, and the anon route must not run authoring machinery).
    body = t.bot_md.split("---", 2)[-1].strip() if t.bot_md.count("---") >= 2 else ""
    return TemplatePreviewOut(
        slug=t.share_slug,
        template_version=t.template_version,
        name=t.name,
        title=t.title,
        author_name=t.author_name,
        description=str(manifest.get("description", "")),
        job=str(manifest.get("job", "")),
        use_when=[str(p) for p in manifest.get("use_when", [])],
        not_for=[str(p) for p in manifest.get("not_for", [])],
        capability=str(manifest.get("capability", "")),
        produces=str(manifest.get("produces", "")),
        tools=[str(x) for x in manifest.get("tools", [])],
        skills=[str(x) for x in manifest.get("skills", [])],
        connectors=[str(x) for x in manifest.get("connectors", [])],
        connector_why={str(k): str(v) for k, v in (manifest.get("connector_why") or {}).items()},
        can_ask_user=bool(manifest.get("can_ask_user", True)),
        instructions=body,
        cases=[
            {"goal": str(c.get("goal", "")), "expect": str(c.get("expect", t.name))}
            for c in manifest.get("cases", [])
        ],
        icon=t.icon,
        icon_hue=t.icon_hue,
        install_count=t.install_count,
        created_at=t.created_at,
    )


class TemplateMineOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    slug: str
    template_version: int
    name: str
    status: str
    install_count: int
    created_at: dt.datetime


def mine_out(t: BotTemplate) -> TemplateMineOut:
    return TemplateMineOut(
        slug=t.share_slug,
        template_version=t.template_version,
        name=t.name,
        status=t.status,
        install_count=t.install_count,
        created_at=t.created_at,
    )


class TemplateInstallNoteOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: str
    detail: str


class TemplateInstallOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    version: int
    notes: list[TemplateInstallNoteOut]
    needs_connect: list[str]


class TemplateInstalledOut(BaseModel):
    """One receipt + the head, for the update chip: `update_available` is computed HERE."""

    model_config = ConfigDict(frozen=True)

    slug: str
    bot_name: str
    installed_version: int
    head_version: int
    update_available: bool
    installed_at: dt.datetime


def installed_out(receipt: BotTemplateInstall, slug: str, head_version: int) -> TemplateInstalledOut:
    installed_version = int((receipt.report or {}).get("template_version", 0) or 0)
    return TemplateInstalledOut(
        slug=slug,
        bot_name=receipt.bot_name,
        installed_version=installed_version,
        head_version=head_version,
        update_available=0 < installed_version < head_version,
        installed_at=receipt.created_at,
    )
