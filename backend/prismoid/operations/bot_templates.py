# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Bot templates - publish a bot as a link, preview it logged out, install it in one click.

The verbs, and who may call them:

* `publish` - the head's AUTHOR (or the project OWNER for a project bot). Only a PUBLISHED head
  can travel: the eval gate already ran, so every shared bot earned green once. Refusals are
  typed: the secret scanner's hits, and unacknowledged external URLs.
* `preview` - NO ACTOR. The fifth route-level auth exception: read-only, single-purpose, the
  consent is the publish act, the capability is the slug's 128 bits, misses are 404s.
* `install` - any authenticated user. One click lands a DRAFT copy in the installer's
  PERSONAL scope, wired to nothing: skills resolve by name or become receipt notes, connector
  slugs resolve against the installer's own rows later, credentials never travel because they
  never were here (Fernet rows are a different table).
* `unpublish` - the author. The link 404s; the rows stay (append-only in spirit).
* `admin_unlist` - the operator's ONE audited verb.
"""

from __future__ import annotations

import uuid

import logfire
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models.actor import Actor
from prismoid.db.models.bot import BotOrigin, BotRecord
from prismoid.db.models.bot_template import BotTemplate
from prismoid.db.models.bot_template_install import BotTemplateInstall
from prismoid.db.models.project import Project, Role
from prismoid.errors import Conflict, NotFound, ValidationFailed
from prismoid.operations import bots as bot_ops
from prismoid.schemas.bot_templates import (
    MAX_TEMPLATE_VERSIONS,
    InstallNote,
    TemplateInstallOutcome,
    TemplateRef,
    TemplateStatus,
    external_urls,
    scan_for_secrets,
)
from prismoid.schemas.bots import BotSource
from prismoid.schemas.ids import new_share_slug


async def publish(
    session: AsyncSession,
    *,
    actor: Actor,
    name: str,
    project_id: uuid.UUID | None = None,
    author_name: str,
    acknowledge_urls: bool = False,
) -> BotTemplate:
    """Snapshot the PUBLISHED head as a template version. Republish under the same name reuses
    the author's slug and appends the next version - the update channel is exactly this."""
    head = await BotRecord.head(session, name, project_id=project_id, owner_actor_id=actor.id)
    if head is None:
        raise NotFound(f"no published bot named {name!r} in this scope - publish the bot first")
    if head.owner_actor_id != actor.id:
        if project_id is None:  # pragma: no cover - the personal predicate is per-owner
            raise NotFound(f"no published bot named {name!r} in this scope")
        await Project.get_for(session, project_id, actor, min_role=Role.OWNER)

    # The snapshot bytes: re-rendered from the row through the ONE format door, so a template
    # is byte-identical to what `plugins export` would write for this version.
    resolved = bot_ops.to_resolved(head)
    manifest_payload = dict(head.manifest or {})
    from prismoid.ai.bots import format as bot_format

    cases, golden = bot_format.cases_of(manifest_payload, default_expect=name)
    bot_md = bot_format.render_bot_md(resolved.manifest, resolved.instructions)
    cases_yaml = bot_format.render_cases(cases, golden)

    # The nets: named secret shapes refuse; external URLs demand an acknowledged
    # publish. Nets, not proofs - the transparency preview and the draft-install posture are
    # the load-bearing layers.
    scanned = "\n".join([resolved.manifest.description, resolved.instructions, cases_yaml])
    if hits := scan_for_secrets(scanned):
        raise ValidationFailed(
            "this bot's text contains what looks like "
            + ", ".join(hits)
            + " - a template is a broadcast, so strip it before sharing",
            fields={"secrets": ", ".join(hits)},
        )
    if (urls := external_urls(scanned)) and not acknowledge_urls:
        raise ValidationFailed(
            "this bot's text contains external URLs - review them and publish again with "
            "acknowledge_urls=true if they belong in a public template: " + ", ".join(urls[:8]),
            fields={"urls": ", ".join(urls[:8])},
        )

    prior = await _slug_for_author(session, actor.id, name)
    slug = prior or new_share_slug()
    version = await BotTemplate.head_version(session, slug) + 1
    if version > MAX_TEMPLATE_VERSIONS:
        raise Conflict(f"{slug} has {version - 1} versions - that is a loop, not an update channel")

    row = BotTemplate(
        id=uuid.uuid4(),
        share_slug=slug,
        template_version=version,
        name=name,
        author_actor_id=actor.id,
        author_name=author_name.strip()[:120],
        bot_md=bot_md,
        cases_yaml=cases_yaml,
        manifest=manifest_payload,
        icon=head.icon,
        icon_hue=head.icon_hue,
        title=head.title,
        source_bot_version=head.version,
    )
    session.add(row)
    await session.commit()
    logfire.info("published template {slug} v{v} from {name}", slug=slug, v=version, name=name)
    return row


async def _slug_for_author(session: AsyncSession, author_id: uuid.UUID, name: str) -> str | None:
    """The author's existing slug for this bot name, so a republish is an update, not a fork."""
    return await BotTemplate.slug_for_author(session, author_id, name)


async def preview(session: AsyncSession, slug: str) -> BotTemplate:
    """NO ACTOR (auth exception #5). Serves only a slug whose head is published."""
    row = await BotTemplate.head(session, slug)
    if row is None:
        raise NotFound("no such template")
    return row


async def install(
    session: AsyncSession,
    *,
    actor: Actor,
    slug: str,
    rename: str | None = None,
) -> tuple[BotRecord, TemplateInstallOutcome]:
    """One click → a DRAFT copy in the installer's personal scope, wired to their own world.

    A name collision with an unrelated personal bot is a 409 (the body may carry a rename);
    reinstalling the SAME template is a new draft version of the same bot, which is exactly
    the update gesture. The installer re-earns green - verdicts never travel."""
    template = await BotTemplate.head(session, slug)
    if template is None:
        raise NotFound("no such template")
    ref = TemplateRef(slug=slug, version=template.template_version)
    name = (rename or template.name).strip()

    existing = await BotRecord.all_versions(session, name, project_id=None, owner_actor_id=actor.id)
    if existing and not any((r.source or "").startswith(f"template:{slug}@") for r in existing):
        raise Conflict(f"you already have a personal bot named {name!r} - install with a different name")

    # Through the ONE push door: the same validation, the same version discipline. A rename
    # rides the format door's name-must-match rule by rewriting the frontmatter name line.
    bot_md = template.bot_md
    if name != template.name:
        bot_md = bot_md.replace(f"name: {template.name}", f"name: {name}", 1)
        # cases expect the bot's own name; retarget the positive cases with it
        cases_yaml = template.cases_yaml.replace(f"expect: {template.name}", f"expect: {name}")
    else:
        cases_yaml = template.cases_yaml
    record = await bot_ops.push(
        session,
        actor=actor,
        name=name,
        source=BotSource(bot_md=bot_md, cases_yaml=cases_yaml),
        project_id=None,
    )
    record.origin = BotOrigin.IMPORTED.value
    record.source = ref.source()
    if template.icon is not None:
        record.icon = template.icon
    if template.icon_hue is not None:
        record.icon_hue = template.icon_hue
    if template.title is not None:
        record.title = template.title

    notes: list[InstallNote] = []
    needs_connect: list[str] = []
    manifest = template.manifest or {}
    skill_names = [str(s) for s in manifest.get("skills", [])]
    if skill_names:
        from prismoid.db.models.skill import SkillRecord

        for skill_name in skill_names:
            if await SkillRecord.head(session, skill_name) is None:
                notes.append(
                    InstallNote(
                        kind="skill_missing",
                        detail=f"the template pins skill {skill_name!r}, which this workspace does not have",
                    )
                )
    slugs = [str(c) for c in manifest.get("connectors", [])]
    if slugs:
        from prismoid.db.models.connector import Connector

        mine = await Connector.resolve_refs(session, actor.id, slugs)
        needs_connect = [s for s in slugs if s not in mine]
        for s in needs_connect:
            notes.append(InstallNote(kind="needs_connect", detail=f"connect {s} to give it its hands"))

    receipt = BotTemplateInstall(
        id=uuid.uuid4(),
        template_id=template.id,
        installer_actor_id=actor.id,
        bot_name=name,
        report={
            "notes": [n.model_dump() for n in notes],
            "needs_connect": needs_connect,
            "template_version": template.template_version,
        },
    )
    session.add(receipt)
    await BotTemplate.bump_install_count(session, template.id)
    await session.commit()
    logfire.info("installed template {slug} v{v} as {name}", slug=slug, v=ref.version, name=name)
    outcome = TemplateInstallOutcome(
        name=name,
        version=record.version,
        notes=tuple(notes),
        needs_connect=tuple(needs_connect),
    )
    return record, outcome


async def unpublish(session: AsyncSession, *, actor: Actor, slug: str) -> None:
    """The author's kill switch: every version of the slug goes unlisted; the link 404s."""
    rows = await BotTemplate.all_for_slug(session, slug, author_actor_id=actor.id)
    if not rows:
        raise NotFound("no such template of yours")
    for row in rows:
        row.status = TemplateStatus.UNLISTED.value
    await session.commit()


async def mine(session: AsyncSession, *, actor: Actor) -> list[BotTemplate]:
    """ "Shared by you" - newest version per slug, newest first."""
    rows = await BotTemplate.heads_for_author(session, actor.id)
    return sorted(rows, key=lambda r: r.created_at, reverse=True)


async def installed(session: AsyncSession, *, actor: Actor) -> list[tuple[BotTemplateInstall, str, int]]:
    """My receipts, each with its slug and the slug's CURRENT head version - the update chip
    is `receipt.report.template_version < head_version`, computed right here at read time."""
    receipts = await BotTemplateInstall.for_installer(session, actor.id)
    # Three queries for the page, never two per receipt: the rows, then the heads.
    templates = await BotTemplate.by_ids(session, [r.template_id for r in receipts])
    heads = await BotTemplate.head_versions(session, sorted({t.share_slug for t in templates.values()}))
    return [
        (receipt, template.share_slug, heads.get(template.share_slug, 0))
        for receipt in receipts
        if (template := templates.get(receipt.template_id)) is not None
    ]


async def admin_unlist(session: AsyncSession, *, admin: Actor, template_id: uuid.UUID, note: str) -> None:
    """The operator's one verb: status → removed, audited. No delete, no edit."""
    from prismoid.operations import admin_ops

    row = await session.get(BotTemplate, template_id)
    if row is None:
        raise NotFound("no such template")
    for r in await BotTemplate.all_for_slug(session, row.share_slug):
        r.status = TemplateStatus.REMOVED.value
    admin_ops.audit(session, admin, "template.unlist", "bot_template", str(template_id), {"note": note})
    await session.commit()
