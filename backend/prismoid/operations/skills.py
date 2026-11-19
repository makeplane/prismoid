# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Skills - the registry composer (M5).

`operations` reads and writes the `skills_registry` rows and the MinIO bodies; `ai/skills/format`
validates a skill and `ai/skills` will load it into a frame. Adding or editing a skill is **data,
not a deploy**: a new version is a row and a content-addressed blob, hot the moment it is written.

The registry is versioned. A builtin is seeded as v1 and **never mutated in place**; editing it is a
new version with `origin` still recording where it came from. That is what makes "the twelve are
editable by anyone" true without ever losing the original.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Collection, Sequence
from pathlib import Path, PurePath

import logfire
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid import storage
from prismoid.ai.agents import grants
from prismoid.ai.skills import deliverables
from prismoid.ai.skills import format as skill_format
from prismoid.ai.skills.format import SkillError
from prismoid.ai.skills.loader import default_loader
from prismoid.ai.tools import catalog
from prismoid.ai.tools.call_rows import JournalTool
from prismoid.ai.tools.skills import SKILL_READ, SkillCard, menu_of
from prismoid.conf import settings
from prismoid.db.models import tenant
from prismoid.db.models.skill import EvalStatus, SkillOrigin, SkillRecord, SkillStatus
from prismoid.errors import Conflict, NotFound, ValidationFailed
from prismoid.schemas.contract import SkillPin
from prismoid.schemas.ids import SkillName
from prismoid.schemas.plan import StepSpec
from prismoid.schemas.skills import (
    GoldenSkillCase,
    ParsedSkill,
    ResolvedSkill,
    RoutingCase,
    SkillFileData,
    SkillManifest,
)

BUILTIN_DIR = deliverables.BUILTIN_DIR  # the builtin sources ride next to the package


async def seed_builtins(session: AsyncSession, source: Path | None = None, *, force: bool = False) -> int:
    """Seed the twelve builtins as v1/published, once. Idempotent, and safe to run on every migrate.

    **Idempotent by (name, v1) presence, not by a flag.** A builtin already at v1 is left exactly as
    it is - re-seeding must never overwrite a v1 that a user has since edited *around* (their v2 is a
    separate row) or, worse, mutate a builtin in place, which the doctrine forbids. So the check
    is "does v1 exist?", and if it does, this skill is skipped. Nothing is thrown away.

    `force=True` is the deliberate exception - `prismoid skills seed --force`, never `just migrate`. It
    drops the builtin rows and re-writes them from `skills/`, which is how a *shape* change to what a
    version stores (routing cases were added to the manifest once) reaches builtins seeded before it. It
    touches only `origin=builtin` rows, so a user's own versions of a builtin are never disturbed.

    Runs as part of `just migrate` (alembic upgrade, then this): the schema is Alembic's, the *data*
    is application code, because a body lives in MinIO by content hash and a SQL migration cannot put
    it there.
    """
    directory = source or BUILTIN_DIR
    if force:
        await SkillRecord.delete_builtins(session)

    seeded = 0
    for child in sorted(directory.iterdir()):
        if not child.is_dir() or not (child / "SKILL.md").exists():
            continue
        parsed = skill_format.parse_dir(child)  # validates against the skill format on the way in

        if await SkillRecord.exists(session, parsed.manifest.name, version=1):
            continue

        await _write_version(
            session, parsed, version=1, status=SkillStatus.PUBLISHED, origin=SkillOrigin.BUILTIN
        )
        seeded += 1

    if seeded:
        await session.commit()
        logfire.info("seeded {n} builtin skill(s)", n=seeded)
    return seeded


async def push(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID,
    name: str,
    skill_md: str,
    cases_yaml: str,
    files: Sequence[tuple[str, bytes]] = (),
    strict: bool = True,
) -> SkillRecord:
    """A new **draft** version from an author's edited skill.

    Parsed through the one door (`format.parse`), so the write-time doctrine is enforced identically to
    the seed - a body over budget or a description missing "Load when" is a `SkillError`, translated
    here into a `ValidationFailed` the API renders as a 422 that names the field. The new version is
    the next integer after the highest existing one, `draft`/`untested`/`origin=user`, attributed to
    its author: a draft resolves only for whoever wrote it, so `created_by` is not optional here.
    """
    try:
        # `strict=False` is the IMPORT door: a third-party bundle validates to the
        # Agent Skills spec and lands as a draft; the house doctrine meets it at publish. The
        # authoring paths (CLI push, HTTP push, seed) never pass the flag, so their 422s - the
        # good ones, naming the rule - stay word-for-word what they were.
        parsed = skill_format.parse(name, skill_md, cases_yaml, files=files, strict=strict)
    except SkillError as exc:
        raise ValidationFailed(str(exc), fields=exc.fields) from exc
    await _refuse_builtin_name(session, name)

    existing = await SkillRecord.all_versions(session, name)
    next_version = (max(v.version for v in existing) + 1) if existing else 1
    record = await _write_version(
        session,
        parsed,
        version=next_version,
        status=SkillStatus.DRAFT,
        origin=SkillOrigin.USER,
        created_by=actor_id,
        workspace_id=tenant.workspace_of(session),  # an authored skill belongs to the author's workspace
    )
    await session.commit()
    logfire.info("pushed {name} draft v{v}", name=name, v=next_version)
    return record


async def list_skills(session: AsyncSession) -> list[SkillRecord]:
    """The published head of every skill - the index a plan's steps choose from."""
    return await SkillRecord.published_heads(session)


async def menu(session: AsyncSession, agent: str) -> tuple[SkillCard, ...]:
    """The skills `agent` reads frameless (`grants.SKILLS`), each at its published head, frozen for one run."""
    granted = grants.SKILLS.get(agent, frozenset())
    if not granted:
        return ()
    heads = await SkillRecord.published_heads(session)
    return menu_of(
        SkillCard(
            name=r.name,
            version=r.version,
            description=str((r.manifest or {}).get("description", "")),
            body_sha256=r.body_sha256,
        )
        for r in heads
        if r.name in granted
    )


async def versions(session: AsyncSession, name: str) -> list[SkillRecord]:
    """Every version of one skill, newest first - the history `skills versions report` prints."""
    return await SkillRecord.all_versions(session, name)


async def resolve(session: AsyncSession, name: str, *, version: int | None = None) -> ResolvedSkill:
    """A (name, version) → the body an executor copies into a frame.

    `version=None` means the **published head** - what `skills pull` gives an author and what a plan
    pins when it names a skill without a version. A concrete version is fetched exactly, so a running
    task that pinned v1 keeps getting v1 after v2 publishes (the reproducibility the pins exist for).
    The row names the version; the loader fetches the body by its content hash, cache-first.
    """
    record = (
        await SkillRecord.head(session, name)
        if version is None
        else await SkillRecord.at_version(session, name, version)
    )
    if record is None:
        at = "" if version is None else f" at v{version}"
        raise NotFound(f"no {'published ' if version is None else ''}skill named {name!r}{at}")

    loaded = await asyncio.to_thread(default_loader().load, record.body_sha256)
    raw = record.manifest or {}
    cases = [RoutingCase(goal=c["goal"], expect=SkillName(c["expect"])) for c in raw.get("cases", [])]
    golden = [GoldenSkillCase.model_validate(g) for g in raw.get("golden", [])]
    files = [
        SkillFileData(
            path=str(f["path"]),
            data=(await asyncio.to_thread(default_loader().load, str(f["sha256"]))).data,
        )
        for f in raw.get("files", [])
    ]
    return ResolvedSkill(
        name=SkillName(record.name),
        version=record.version,
        manifest=SkillManifest.model_validate(raw),
        body=loaded.data.decode(),
        routing_cases=cases,
        golden_cases=golden,
        files=files,
    )


async def apply_eval(session: AsyncSession, name: str, *, version: int, green: bool) -> None:
    """Record a **draft's** eval verdict - the column `publish` reads to allow or 409.

    Only a draft's status moves. A published version's verdict is frozen: it passed to ship, and you do
    not un-ship it by re-running the eval - you push a new draft and fix it there. So re-testing a
    published builtin *reports* its routing without downgrading the green it earned at publish."""
    record = await SkillRecord.at_version(session, name, version)
    if record is not None and record.status == SkillStatus.DRAFT.value:
        record.eval_status = EvalStatus.GREEN.value if green else EvalStatus.RED.value
        await session.commit()


async def publish(session: AsyncSession, *, actor_id: uuid.UUID, name: str) -> SkillRecord:
    """Promote the author's latest draft to published - **if its evals are green**.

    Green is an **HTTP 409, not a review comment**: publish reads `eval_status` and refuses a red or
    untested draft, which is only possible because the verdict is a column `test` already wrote. It is
    author-scoped (you publish your own draft), and it affects **new tasks only** - a running task
    pinned `report@1` at submit keeps loading `report@1`, so publishing `report@2` never
    disturbs work in flight. The old published version stays published; the head is simply the newer.
    """
    await _refuse_builtin_name(session, name)  # before the draft: a pre-0b draft may hold it
    draft = await SkillRecord.latest_draft(session, name, actor_id)
    if draft is None:
        raise NotFound(f"no draft of {name!r} to publish - push one first")
    # The doctrine gate. The AUTHORING door enforces the house description shape at
    # parse time; the IMPORT door validates to the Agent Skills spec and lands drafts. Publish is
    # where the two lanes meet, so the doctrine is re-asserted HERE - an imported draft with a
    # spec-legal but doctrine-shy description gets a 409 naming the fix, and no published skill
    # is ever less sharp than the Lead needs. Parse to the spec, gate on publish.
    try:
        skill_format.check_doctrine(name, str((draft.manifest or {}).get("description", "")))
    except SkillError as exc:
        raise Conflict(f"cannot publish: {exc} - edit the draft and push again") from exc
    if draft.eval_status != EvalStatus.GREEN.value:
        raise Conflict(
            f"{name} v{draft.version} is {draft.eval_status}, not green - publishing needs green evals "
            f"Run `prismoid skills test {name} --version {draft.version}` and sharpen it until it "
            "passes."
        )
    draft.status = SkillStatus.PUBLISHED.value
    await session.commit()
    logfire.info("published {name} v{v}", name=name, v=draft.version)
    return draft


async def _refuse_builtin_name(session: AsyncSession, name: str) -> None:
    """A workspace skill may not shadow a builtin's name: drafting or publishing one is a 409."""
    if await SkillRecord.builtin_holds(session, name):
        raise Conflict("that name belongs to a built-in skill")


async def resolve_ref(session: AsyncSession, ref: str, *, actor_id: uuid.UUID | None = None) -> SkillPin:
    """A skill **ref** - `"report"`, `"report@3"`, or `"report@draft"` - → a version-pinned `SkillPin`.

    Resolving to a concrete version *now*, at submit time, is the reproducibility guarantee: a task
    that asked for `report` is pinned to whatever version is the head today, and keeps loading exactly
    that even after a newer one publishes. `@draft` is the **author-only** path: it
    resolves to *your* latest draft, and to nobody else's - a second user's `@draft` is a 404, because
    the draft is not theirs to run, and `@N` naming a draft holds the same line (`pinnable_by`). Once
    pinned the task loads it like any other version; the author boundary is at pin *creation*, here.
    """
    name, _, tag = ref.partition("@")
    if tag == "draft":
        if actor_id is None:
            raise NotFound(f"{ref}: a draft pin is author-only, and there is no author in this context")
        draft = await SkillRecord.latest_draft(session, name, actor_id)
        if draft is None:
            raise NotFound(f"no draft of {name!r} you can pin - push one, or it is not yours")
        return SkillPin(name=SkillName(name), version=draft.version)
    if tag:
        try:
            version = int(tag)
        except ValueError as exc:
            raise ValidationFailed(f"{ref}: version must be an integer or 'draft'", {"skill": ref}) from exc
        record = await SkillRecord.at_version(session, name, version)
    else:
        record = await SkillRecord.head(session, name)
    if record is None or not record.pinnable_by(actor_id):
        raise NotFound(f"no such skill version: {ref!r}")
    return SkillPin(name=SkillName(name), version=record.version)


async def needs(session: AsyncSession, pin: SkillPin) -> tuple[str, ...]:
    """The tools a pinned version declares it needs (`allowed-tools`), read from its row alone."""
    name = str(pin.name)
    record = (
        await SkillRecord.head(session, name)
        if pin.version is None
        else await SkillRecord.at_version(session, name, pin.version)
    )
    if record is None:
        raise NotFound(f"no such skill version: {pin.ref()!r}")
    return SkillManifest.model_validate(record.manifest or {}).allowed_tools


async def pin_format(session: AsyncSession, spec: StepSpec, doc_format: str | None) -> StepSpec:
    """A format maker's `spec` with exactly one format skill pinned (its file name's format, else one
    already pinned for the kind, else the task's); a step publishing no typed file is unchanged."""
    output = spec.contract.output if spec.contract is not None else None
    if output is None:
        return spec
    others = [pin for pin in spec.skill_pins if deliverables.of_skill(pin.name) is None]
    pinned = [
        made.extension
        for pin in spec.skill_pins
        if (made := deliverables.of_skill(pin.name)) and made.kind is output.type
    ]
    named = PurePath(output.filename or "").suffix.lstrip(".").lower()
    made = deliverables.format_for(output.type, named or next(iter(pinned), "") or doc_format)
    if made is None:
        return spec
    kept = next((pin for pin in spec.skill_pins if pin.name == made.skill), None)
    try:
        pin = kept or await resolve_ref(session, made.skill)
    except NotFound:  # fail-soft: an unseeded registry costs the craft, never the step
        logfire.warn(
            "format skill {skill} is not in the registry; the step runs without it", skill=made.skill
        )
        return spec
    return spec.model_copy(update={"skill_pins": [*others, pin]})


async def honoured(
    session: AsyncSession, spec: StepSpec, tools: Collection[str], journal: JournalTool | None
) -> StepSpec:
    """`spec` with only the pins an agent holding `tools` can follow (`grants.skill_fits`); each refused
    pin is journaled with the tools it lacks, before anything mounts or inlines it."""
    kept: list[SkillPin] = []
    for pin in spec.skill_pins:
        declared = await needs(session, pin)
        if grants.skill_fits(tools, declared):
            kept.append(pin)
            continue
        lacking = sorted(set(declared) - {catalog.model_name(name) for name in tools})
        reason = f"{spec.agent} does not hold {', '.join(lacking)}, which the skill needs"
        if journal is not None:
            await journal(catalog.tool(SKILL_READ).journal, False, summary=pin.ref(), denied_reason=reason)
    if len(kept) == len(spec.skill_pins):
        return spec
    return spec.model_copy(update={"skill_pins": kept})


async def resolve_pin(session: AsyncSession, pin: SkillPin) -> ResolvedSkill:
    """A plan's `SkillPin` → its body. `version=None` on a pin means the author's draft; draft
    resolution is the author-only path, not wired yet, so for now a `None` pin resolves to the
    published head - the same skill a plan-time resolver will pin explicitly once pinning is wired."""
    return await resolve(session, str(pin.name), version=pin.version)


async def _write_version(
    session: AsyncSession,
    parsed: ParsedSkill,
    *,
    version: int,
    status: SkillStatus,
    origin: SkillOrigin,
    created_by: uuid.UUID | None = None,
    workspace_id: uuid.UUID | None = None,
) -> SkillRecord:
    """Persist one skill version: body to MinIO (content-addressed), manifest + cases to the row.

    The routing cases ride in the manifest JSONB under a `cases` key rather than a column of their own:
    they are small, they version with the skill, and keeping them in the row means `resolve` restores a
    *whole* skill directory - `SKILL.md` and `cases.yaml` - without a second fetch, which is what lets
    `pull` round-trip a builtin the author never had on disk."""
    body_sha = await storage.aput(parsed.body.encode(), bucket=settings.worker.skill_bucket)
    manifest = parsed.manifest.model_copy(update={"body_sha256": body_sha})
    manifest_json = manifest.model_dump(mode="json")
    manifest_json["cases"] = [{"goal": c.goal, "expect": str(c.expect)} for c in parsed.routing_cases]
    manifest_json["golden"] = [g.model_dump(mode="json") for g in parsed.golden_cases]
    # Bundled files: one CAS blob per file - dedupe and the hash-keyed loader cache
    # work unchanged - with the pointer list in the manifest JSONB (no migration, restores whole).
    manifest_json["files"] = [
        {
            "path": f.path,
            "sha256": await storage.aput(f.data, bucket=settings.worker.skill_bucket),
            "size": len(f.data),
        }
        for f in parsed.files
    ]

    record = SkillRecord(
        id=uuid.uuid4(),
        name=str(parsed.manifest.name),
        version=version,
        status=status.value,
        origin=origin.value,
        manifest=manifest_json,
        body_sha256=body_sha,
        body_key=body_sha,
        eval_status=EvalStatus.UNTESTED.value if status is SkillStatus.DRAFT else EvalStatus.GREEN.value,
        created_by=created_by,
        workspace_id=workspace_id,  # NULL = a platform builtin; the skills_registry listener never stamps
    )
    session.add(record)
    await session.flush()
    return record
