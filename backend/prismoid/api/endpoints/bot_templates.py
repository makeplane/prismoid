# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Bot templates - publish, preview, install (ADR-056).

`get_template` is the FIFTH route-level auth exception (the cr_/cn_/rt_ family, and the first
anonymous READ): a share link must open logged out or it is not a share link. Read-only,
single-purpose, consent = the author's publish act, capability = the slug's 128 bits, misses
are 404s, per-IP throttled, `X-Robots-Tag: noindex` while unlisted-by-default.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import Request, Response

from prismoid.api.deps import ActorDep, AdminDep, SessionDep
from prismoid.api.serializers.bot_templates import (
    TemplateInstalledOut,
    TemplateInstallIn,
    TemplateInstallNoteOut,
    TemplateInstallOut,
    TemplateMineOut,
    TemplatePreviewOut,
    TemplatePublishIn,
    installed_out,
    mine_out,
    preview_out,
)
from prismoid.operations import bot_templates as ops
from prismoid.operations.throttle import FixedWindowLimiter

_preview_hits = FixedWindowLimiter("template preview")


async def publish_template(body: TemplatePublishIn, actor: ActorDep, session: SessionDep) -> TemplateMineOut:
    row = await ops.publish(
        session,
        actor=actor,
        name=body.name,
        project_id=body.project_id,
        author_name=body.author_name,
        acknowledge_urls=body.acknowledge_urls,
    )
    return mine_out(row)


async def list_my_templates(actor: ActorDep, session: SessionDep) -> list[TemplateMineOut]:
    return [mine_out(t) for t in await ops.mine(session, actor=actor)]


async def list_installed(actor: ActorDep, session: SessionDep) -> list[TemplateInstalledOut]:
    rows = await ops.installed(session, actor=actor)
    return [installed_out(receipt, slug, head_v) for receipt, slug, head_v in rows]


async def get_template(
    slug: str, request: Request, response: Response, session: SessionDep
) -> TemplatePreviewOut:
    _preview_hits.hit(request.client.host if request.client else "unknown", per_minute=60)
    response.headers["X-Robots-Tag"] = "noindex"
    return preview_out(await ops.preview(session, slug))


async def install_template(
    slug: str, body: TemplateInstallIn, actor: ActorDep, session: SessionDep
) -> TemplateInstallOut:
    _record, outcome = await ops.install(session, actor=actor, slug=slug, rename=body.rename)
    return TemplateInstallOut(
        name=outcome.name,
        version=outcome.version,
        notes=[TemplateInstallNoteOut(kind=n.kind, detail=n.detail) for n in outcome.notes],
        needs_connect=list(outcome.needs_connect),
    )


async def unpublish_template(slug: str, actor: ActorDep, session: SessionDep) -> dict[str, str]:
    await ops.unpublish(session, actor=actor, slug=slug)
    return {"status": "unlisted"}


async def admin_unlist_template(
    template_id: UUID, actor: AdminDep, session: SessionDep, note: str = ""
) -> dict[str, str]:
    await ops.admin_unlist(session, admin=actor, template_id=template_id, note=note[:400])
    return {"status": "removed"}
