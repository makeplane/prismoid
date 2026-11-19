# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Bots - user-authored agents as data. draft → test → publish, scoped to a Project or a person.

The composer for `api/endpoints/bots.py`, `runner/bots_cli.py`, and the roster One plans against.
It is the only place that holds all three halves at once: the row (`db/models/bot.py`), the format
door (`ai/bots/format.py`), and the compiler (`ai/agents/bot_compile.py`). Views may not reach the
engine and the engine may not reach the database, so this file is where a stored
bot becomes a runnable agent - and it hands **values** upward, never rows into prompts.

Its neighbours each hold one job: `bot_scope` (who may touch a bot, which version an edit means),
`bot_selection` (the eval that gates publishing), `bot_lessons` (teach and draft) and `bot_access`
(the access tab).

Two rules carry most of the weight here:

* **Publishing requires green evals, as a 409** - not a review comment. Only possible
  because `eval_status` is a column that `bot_selection.test_selection` already wrote.
* **A bot may never take a built-in's name.** Refused here at create time so an author gets a
  422 they can act on, and refused again at compile time (`ai/agents/bot_compile.py`) so a built-in
  added *later* still wins. Two checks, because they fail at different times.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from enum import StrEnum
from typing import TYPE_CHECKING

import logfire
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid import storage
from prismoid.ai.bots import format as bot_format
from prismoid.ai.bots.format import BotError
from prismoid.ai.context.thread import render_thread_md
from prismoid.db.base import utcnow
from prismoid.db.models import tenant
from prismoid.db.models.actor import Actor
from prismoid.db.models.artifact import Artifact
from prismoid.db.models.bot import BotOrigin, BotRecord, BotStatus
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.memory import Memory, MemoryScope
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.project import Project, Role
from prismoid.db.models.routine import Routine
from prismoid.db.models.skill import EvalStatus
from prismoid.db.models.task import Task
from prismoid.db.models.verification import Verification
from prismoid.errors import Conflict, NotFound, ValidationFailed
from prismoid.operations import allowance, dispatch
from prismoid.operations.bot_scope import authorize, require_scorable, working_record
from prismoid.schemas.bots import (
    MAX_DESCRIPTION_CHARS,
    MAX_ROSTER_BOTS,
    BotManifest,
    BotSource,
    BotStats,
    DigestArtifact,
    DigestStep,
    ParsedBot,
    ResolvedBot,
    RunDigest,
    SelectionReport,
    compose_description,
)
from prismoid.schemas.contract import BotPin
from prismoid.schemas.events import ToolCalled
from prismoid.schemas.ids import ActorId, BotName, SkillName
from prismoid.schemas.model_class import ModelClass
from prismoid.schemas.outputs import ArtifactType

if TYPE_CHECKING:
    from collections.abc import Iterable


def _reserved_reason(name: str) -> str | None:
    """Why this name cannot be a bot, or None. Imports the registry lazily: `operations` is the
    composer and may reach the engine, but only a call that needs the roster should pay for
    loading it."""
    from prismoid.ai.agents.base import roster

    if any(str(a.name) == name for a in roster()):
        return (
            f"{name!r} is a built-in agent. Pick another name - a bot that shadowed one would be "
            "unreachable, because the built-in always wins the lookup."
        )
    return None


# ── the write path ────────────────────────────────────────────────────────────────────────────


async def push(
    session: AsyncSession,
    *,
    actor: Actor,
    name: str,
    source: BotSource,
    project_id: uuid.UUID | None = None,
) -> BotRecord:
    """A new **draft** version from an author's edited bot.

    Every push is a new version - versions are immutable, which is what makes a pin mean
    something. `created_by` is not optional: a draft resolves only for whoever wrote it, so a row
    with no author is a row nobody can publish.
    """
    await authorize(session, actor, project_id, min_role=Role.EDITOR)
    bot_md, cases_yaml = _render(name, source)
    try:
        parsed = bot_format.parse(name, bot_md, cases_yaml)
    except BotError as exc:
        raise ValidationFailed(str(exc), fields=exc.fields) from exc

    if (reason := _reserved_reason(name)) is not None:
        raise ValidationFailed(reason, fields={"name": "taken by a built-in"})

    existing = await BotRecord.all_versions(session, name, project_id=project_id, owner_actor_id=actor.id)
    # A name already used by someone ELSE in this project is theirs; a new version of it would be an
    # impersonation, not an edit. (Personal scope cannot collide - the predicate is per-owner.)
    if project_id is not None and existing and all(row.owner_actor_id != actor.id for row in existing):
        owner_ids = {row.owner_actor_id for row in existing}
        if actor.id not in owner_ids:
            await Project.get_for(session, project_id, actor, min_role=Role.OWNER)

    version = (max(row.version for row in existing) + 1) if existing else 1
    record = _write_version(
        parsed,
        version=version,
        owner_actor_id=actor.id,
        project_id=project_id,
        status=BotStatus.DRAFT,
        origin=BotOrigin.USER,
    )
    if existing:
        _carry_per_name(existing[0], record)
    session.add(record)
    await session.commit()
    logfire.info("pushed bot {name} draft v{v}", name=name, v=version)
    return record


# The PER-NAME columns (the face, the pin, the work dials, the threads dial): settings kept
# identical across a name's versions by `set_face`, never authored judgment. A new version
# inherits them from the newest row, or a publish would reset an owner's `work_mode=never`
# to the column default (found in review).
_PER_NAME_COLUMNS = ("icon", "icon_hue", "title", "pinned", "work_mode", "task_budget_usd", "threads")


def _carry_per_name(newest: BotRecord, record: BotRecord) -> None:
    for column in _PER_NAME_COLUMNS:
        setattr(record, column, getattr(newest, column))


def _render(name: str, source: BotSource) -> tuple[str, str]:
    """A `BotSource` → the `(BOT.md, cases.yaml)` bytes the one format door parses.

    Raw wins. Otherwise the fields are rendered - and rendered through `ai/bots/format`, never by
    string concatenation, because a description contains `Not for: …` and an unquoted YAML scalar
    with a `: ` in it reads back as a mapping. The skills lane paid for that bug once already.
    """
    if source.bot_md is not None:
        return source.bot_md, source.cases_yaml
    manifest = _manifest_of(name, _description_of(source), source)
    return (
        bot_format.render_bot_md(manifest, source.instructions or ""),
        bot_format.render_cases(source.cases, source.golden),
    )


def _description_of(source: BotSource) -> str | None:
    """The verbatim description, else the one composed from the structured triple, else None."""
    description = source.description
    if description is not None or not (source.job or source.use_when or source.not_for):
        return description
    # The structured half: the form typed meaning; the one composer writes the
    # grammar. A missing piece is a 422 naming that piece, not a lecture about "Load when".
    try:
        description = compose_description(source.job or "", source.use_when, source.not_for)
    except ValueError as exc:
        raise ValidationFailed(str(exc), fields={"description": str(exc)}) from exc
    # The per-field caps allow a composition past the manifest's own cap (found in review:
    # 240 + 4x100 + 4x100 characters compose to ~1075 against 600, a bare pydantic 500).
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise ValidationFailed(
            f"the job and phrases compose to {len(description)} characters, over the "
            f"{MAX_DESCRIPTION_CHARS} limit - shorten them",
            fields={"job": f"composed description {len(description)} chars > {MAX_DESCRIPTION_CHARS}"},
        )
    return description


def _manifest_of(name: str, description: str | None, source: BotSource) -> BotManifest:
    """The routing surface, validated. Nothing but the name is mandatory at birth: a bare create
    renders an empty-but-valid draft, and PUBLISH names whatever is still missing."""
    try:
        return BotManifest(
            name=BotName(name),
            description=description or "",
            job=" ".join((source.job or "").split()),
            use_when=source.use_when,
            not_for=source.not_for,
            capability=_enum(ModelClass, source.capability, ModelClass.FAST_EXECUTOR),
            produces=_enum(ArtifactType, source.produces, ArtifactType.DOCUMENT),
            tools=source.tools,
            skills=tuple(SkillName(s) for s in source.skills),
            connectors=source.connectors,
            connector_why=dict(source.connector_why),
            can_ask_user=source.can_ask_user,
        )
    except PydanticValidationError as exc:  # a cap the door rules missed still 422s by name
        first = exc.errors()[0] if exc.errors() else {}
        field = ".".join(str(p) for p in first.get("loc", ())) or "manifest"
        raise ValidationFailed(
            f"{field}: {first.get('msg', 'invalid')}", fields={field: str(first.get("msg", "invalid"))[:200]}
        ) from exc


def _enum[T: StrEnum](kind: type[T], raw: str | None, fallback: T) -> T:
    """Lenient on purpose: an unknown value falls through to the format door, which names the real
    problem with the real list of alternatives. Raising here would give a worse message twice."""
    try:
        return kind(raw) if raw else fallback
    except ValueError:
        return fallback


def _write_version(
    parsed: ParsedBot,
    *,
    version: int,
    owner_actor_id: uuid.UUID,
    project_id: uuid.UUID | None,
    status: BotStatus,
    origin: BotOrigin,
    source: str | None = None,
) -> BotRecord:
    """The row, assembled. The manifest JSONB carries the routing surface AND the cases, so the
    roster query and the eval both read one row - the pattern `skills_registry` set with
    `manifest["cases"]`, and the reason choosing a bot never costs a second fetch."""
    sha = storage.sha256(parsed.instructions.encode())
    manifest = parsed.manifest.model_copy(update={"instructions_sha256": sha})
    payload = manifest.model_dump(mode="json")
    payload["cases"] = [{"goal": c.goal, "expect": c.expect} for c in parsed.cases]
    if parsed.golden:
        payload["golden"] = [g.model_dump(mode="json") for g in parsed.golden]
    return BotRecord(
        id=uuid.uuid4(),
        name=str(manifest.name),
        version=version,
        status=status.value,
        origin=origin.value,
        owner_actor_id=owner_actor_id,
        project_id=project_id,
        manifest=payload,
        instructions=parsed.instructions,
        instructions_sha256=sha,
        eval_status=EvalStatus.UNTESTED.value,
        source=source,
    )


async def publish(
    session: AsyncSession, *, actor: Actor, name: str, project_id: uuid.UUID | None = None
) -> BotRecord:
    """Promote the author's latest draft to published - **if its evals are green**.

    Green is an **HTTP 409, not a review comment**: publish reads `eval_status` and refuses a red
    or untested draft, which is only possible because the verdict is a column `test` already
    wrote. A bot whose description cannot win its own goals will misroute silently in production,
    and the whole point of a roster is that One can trust what it reads.
    """
    await authorize(session, actor, project_id, min_role=Role.EDITOR)
    draft = await BotRecord.latest_draft(session, name, actor.id, project_id=project_id)
    if draft is None:
        raise NotFound(f"no draft of {name!r} to publish - push one first")
    manifest = draft.manifest or {}
    if not str(manifest.get("description", "")).strip():
        raise Conflict(
            f"{name} v{draft.version} has no routing description yet - answer the three questions "
            "on Configure (what it does, when it steps in, what it refuses), then test and publish."
        )
    if not draft.instructions.strip():
        raise Conflict(
            f"{name} v{draft.version} has no instructions yet - write them, or press "
            '"Draft for me" on Configure, then test and publish.'
        )
    if not manifest.get("cases"):
        raise Conflict(
            f"{name} v{draft.version} has no selection cases yet - add at least one goal it "
            "should win (Configure), then test and publish."
        )
    if draft.eval_status != EvalStatus.GREEN.value:
        raise Conflict(
            f"{name} v{draft.version} is {draft.eval_status}, not green - publishing needs green "
            f"selection evals. Run `prismoid bots test {name}` and sharpen the description until "
            "it wins its own goals and refuses the others."
        )
    draft.status = BotStatus.PUBLISHED.value
    await session.commit()
    logfire.info("published bot {name} v{v}", name=name, v=draft.version)
    return draft


async def archive(
    session: AsyncSession, *, actor: Actor, name: str, project_id: uuid.UUID | None = None
) -> BotRecord:
    """Retire the published head. Archived, never deleted: a task that pinned it must still be
    able to explain what ran (Rule 10's spirit - the record is append-only)."""
    await authorize(session, actor, project_id, min_role=Role.OWNER if project_id else Role.EDITOR)
    head = await BotRecord.head(session, name, project_id=project_id, owner_actor_id=actor.id)
    if head is None:
        raise NotFound(f"no published {name!r} to archive")
    head.status = BotStatus.ARCHIVED.value
    await session.commit()
    logfire.info("archived bot {name} v{v}", name=name, v=head.version)
    return head


async def apply_eval(
    session: AsyncSession,
    *,
    actor: Actor,
    name: str,
    version: int,
    report: SelectionReport,
    project_id: uuid.UUID | None = None,
) -> None:
    """Record a **draft's** verdict - the column `publish` reads to allow or 409.

    Only a draft's status moves. A published version's verdict is frozen: it passed when it was
    published, and re-scoring it later against a roster that has since changed would rewrite
    history to explain the present.
    """
    record = await BotRecord.at_version(
        session, name, version, project_id=project_id, owner_actor_id=actor.id
    )
    if record is not None and record.status == BotStatus.DRAFT.value:
        record.eval_status = EvalStatus.GREEN.value if report.green else EvalStatus.RED.value
        record.eval_report = report.model_dump(mode="json")
        await session.commit()


async def set_face(
    session: AsyncSession,
    *,
    actor: Actor,
    name: str,
    icon: str | None = None,
    icon_hue: int | None = None,
    title: str | None = None,
    clear_icon: bool = False,
    clear_hue: bool = False,
    clear_title: bool = False,
    work_mode: str | None = None,
    task_budget_usd: float | None = None,
    threads: str | None = None,
    project_id: uuid.UUID | None = None,
) -> BotRecord:
    """The bot's face - icon + hue + title, its work dials and its threads dial.
    PER BOT: applied to every version of
    the name in scope, because an avatar rename must neither mint a draft nor leave old
    versions wearing yesterday's face. EDITOR, like any edit; the stored string is never
    interpreted server-side (the icon-picker law)."""
    await authorize(session, actor, project_id, min_role=Role.EDITOR)
    rows = await BotRecord.all_versions(session, name, project_id=project_id, owner_actor_id=actor.id)
    if not rows:
        raise NotFound(f"no bot named {name!r} in this scope")
    for row in rows:
        if clear_icon:
            row.icon = None
        elif icon is not None:
            row.icon = icon[:80]
        if clear_hue:
            row.icon_hue = None
        elif icon_hue is not None:
            row.icon_hue = int(icon_hue) % 8
        if clear_title:
            row.title = None
        elif title is not None:
            row.title = title.strip()[:120] or None
        # The work dials ride the face PATCH: per name, never a new version -
        # whether a bot may start work by itself is a setting, not authored judgment.
        if work_mode is not None:
            row.work_mode = work_mode
        if task_budget_usd is not None:
            row.task_budget_usd = max(0.0, min(float(task_budget_usd), 500.0))
        if threads is not None:
            row.threads = threads
    await session.commit()
    return rows[0]


# ── the read path ─────────────────────────────────────────────────────────────────────────────


async def list_bots(
    session: AsyncSession,
    actor: Actor,
    *,
    project_id: uuid.UUID | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> list[BotRecord]:
    await authorize(session, actor, project_id, min_role=Role.VIEWER)
    return await BotRecord.listing(
        session, project_id=project_id, owner_actor_id=actor.id, limit=limit, cursor=cursor
    )


async def versions(
    session: AsyncSession, actor: Actor, name: str, *, project_id: uuid.UUID | None = None
) -> list[BotRecord]:
    await authorize(session, actor, project_id, min_role=Role.VIEWER)
    return await BotRecord.all_versions(session, name, project_id=project_id, owner_actor_id=actor.id)


def to_resolved(record: BotRecord) -> ResolvedBot:
    """A row → the frozen value the engine is handed. **The one place a bot row stops being a row**
    (Rule 6): everything above this line may hold a `BotRecord`, everything below values only."""
    payload = dict(record.manifest or {})
    payload.pop("cases", None)
    payload.pop("golden", None)  # the nightly lane's business, not the engine's
    return ResolvedBot(
        name=BotName(record.name),
        version=record.version,
        manifest=BotManifest.model_validate(payload),
        instructions=record.instructions,
        owner_actor_id=ActorId(record.owner_actor_id),
    )


async def resolve(
    session: AsyncSession,
    actor: Actor,
    name: str,
    *,
    version: int | None = None,
    project_id: uuid.UUID | None = None,
) -> ResolvedBot:
    """One bot version as a value. `version=None` ⇒ the published head."""
    await authorize(session, actor, project_id, min_role=Role.VIEWER)
    record = (
        await BotRecord.at_version(session, name, version, project_id=project_id, owner_actor_id=actor.id)
        if version is not None
        else await BotRecord.head(session, name, project_id=project_id, owner_actor_id=actor.id)
    )
    if record is None:
        raise NotFound(f"no such bot version: {name}@{version or 'head'}")
    return to_resolved(record)


async def resolve_ref(
    session: AsyncSession, ref: str, *, actor: Actor, project_id: uuid.UUID | None = None
) -> BotPin:
    """A bot **ref** - `"reviewer"`, `"reviewer@3"`, or `"reviewer@draft"` - → a version-pinned
    `BotPin`. A draft is author-only, as `@draft` or by number: anyone else finds nothing (a 404,
    not someone else's unfinished work)."""
    await authorize(session, actor, project_id, min_role=Role.VIEWER)
    name, _, tag = ref.partition("@")
    if tag == "draft":
        draft = await BotRecord.latest_draft(session, name, actor.id, project_id=project_id)
        if draft is None:
            raise NotFound(f"no draft of {name!r} you can pin - push one, or it is not yours")
        return BotPin(name=BotName(name), version=draft.version)
    if tag:
        try:
            version = int(tag)
        except ValueError as exc:
            raise ValidationFailed(f"{ref}: version must be an integer or 'draft'", {"bot": ref}) from exc
        record = await BotRecord.at_version(
            session, name, version, project_id=project_id, owner_actor_id=actor.id
        )
    else:
        record = await BotRecord.head(session, name, project_id=project_id, owner_actor_id=actor.id)
    if record is None or not record.pinnable_by(actor.id):
        raise NotFound(f"no such bot version: {ref!r}")
    return BotPin(name=BotName(name), version=record.version)


async def roster_for_task(
    session: AsyncSession,
    actor: Actor,
    *,
    project_id: uuid.UUID | None = None,
    pins: Sequence[BotPin] = (),
) -> list[ResolvedBot]:
    """The bots One is shown for one task, as values.

    **Auto-included, not attached.** A roster you must remember to attach is not a roster - the
    whole promise of a bot is that it is *there* when its work arrives. So a task sees the
    published bots of its Project plus the actor's own personal ones, and explicit `pins` (the
    task's `bots:` field) override that set entirely for precise control.

    Capped at `MAX_ROSTER_BOTS`, and the truncation is **logged, never silent**: a roster that
    quietly dropped the bot you were counting on reads as "the bot did not work". Past the cap
    the honest answer is a shortlist pass before the planner, which is the named scale valve.
    """
    if pins:
        resolved: list[ResolvedBot] = []
        for pin in pins:
            resolved.append(
                await resolve(session, actor, str(pin.name), version=pin.version, project_id=project_id)
            )
        return resolved

    heads = await BotRecord.published_heads(session, project_id=project_id, owner_actor_id=actor.id)
    if len(heads) > MAX_ROSTER_BOTS:
        dropped = [row.name for row in heads[MAX_ROSTER_BOTS:]]
        logfire.warn(
            "roster capped at {cap} bots; {n} not shown to the planner: {dropped}",
            cap=MAX_ROSTER_BOTS,
            n=len(dropped),
            dropped=", ".join(dropped),
        )
        heads = heads[:MAX_ROSTER_BOTS]
    return [to_resolved(row) for row in heads]


# ── teaching: the evidence a lesson reads ─────────────────────────────────────────────────────


async def digest_run(session: AsyncSession, actor: Actor, task_id: uuid.UUID) -> RunDigest:
    """One finished run gathered from the durable record (plan, transcript, journal, verdicts): the evidence a
    `from_run` lesson reads."""
    from prismoid.operations import thread as thread_ops  # the engine layer; a noun asks it lazily

    task = await Task.get_for(session, task_id, actor)  # 404s a stranger; never leaks existence

    steps = _digest_steps(await PlanStep.for_task(session, task.id))

    transcript = await thread_ops.render_thread(session, task, budget_tokens=12_000)

    tool_counts, denials = _tool_rollup(await TaskEvent.of_types(session, task.id, ("tool.called",)))

    artifacts: list[DigestArtifact] = []
    for artifact in await Artifact.latest_for_task(session, task.id):
        if artifact.role == "superseded":
            continue  # a steer overtook it: never what the run made
        verification = await Verification.latest_for(session, artifact.id)
        artifacts.append(_digest_artifact(artifact, verification))

    return RunDigest(
        task_id=str(task.id),
        goal=str(task.goal),
        status=str(task.status),
        steps=tuple(steps),
        transcript_md=render_thread_md(transcript),
        tool_counts=tool_counts,
        denials=tuple(denials[:12]),
        artifacts=tuple(artifacts),
    )


def _digest_steps(rows: Iterable[PlanStep]) -> list[DigestStep]:
    """The plan's real steps (not its joins or gates): who ran what, how it ended, and why."""
    steps: list[DigestStep] = []
    for row in rows:
        spec = row.spec or {}
        if spec.get("kind") != "step":
            continue
        steps.append(
            DigestStep(
                agent=str(spec.get("agent") or ""),
                title=str(row.title),
                state=str(row.state),
                why=str(spec.get("why") or ""),
            )
        )
    return steps


def _tool_rollup(events: Iterable[object]) -> tuple[dict[str, int], list[str]]:
    """Calls per tool, and each distinct refusal or failure once, in the order it happened."""
    tool_counts: dict[str, int] = {}
    denials: list[str] = []
    for event in events:
        if not isinstance(event, ToolCalled):  # pragma: no cover - the type filter guarantees it
            continue
        tool_counts[event.tool] = tool_counts.get(event.tool, 0) + 1
        reason = f"{event.tool}: {event.denied_reason or 'failed'}"
        if not event.ok and reason not in denials:
            denials.append(reason)
    return tool_counts, denials


def _digest_artifact(artifact: Artifact, verification: Verification | None) -> DigestArtifact:
    """A deliverable with its verdict and its first failed checks."""
    findings: list[str] = []
    if verification is not None:
        checks = (verification.report or {}).get("checks", [])
        findings = [
            f"{check.get('name')}: {str(check.get('detail'))[:200]}"
            for check in checks
            if str(check.get("status")) == "fail"
        ]
    return DigestArtifact(
        filename=str(artifact.provenance.get("path", artifact.title)).rsplit("/", 1)[-1],
        type=str(artifact.type),
        verdict=str(verification.verdict) if verification is not None else "",
        findings=tuple(findings[:6]),
    )


async def request_draft(
    session: AsyncSession,
    *,
    actor: Actor,
    name: str,
    project_id: uuid.UUID | None = None,
) -> str:
    """Enqueue a `from_idea` lesson on the worker and return its job key; the deterministic
    `botdraft-{row_id}` key dedupes a double click."""
    await authorize(session, actor, project_id, min_role=Role.EDITOR)
    await allowance.require(session, tenant.workspace_of(session), actor=actor)  # the job spends
    record = await working_record(session, actor, name, project_id=project_id)
    key = _studio_job_key("botdraft", record.id)
    await dispatch.get_dispatcher().enqueue(
        "draft_bot",
        key,
        actor_id=str(actor.id),
        name=name,
        project_id=str(project_id) if project_id else None,
        workspace_id=str(record.workspace_id),  # the job acts for the bot's workspace
    )
    return key


def _studio_job_key(kind: str, row_id: uuid.UUID) -> str:
    """`bottest-{row}~{minute}` / `botdraft-{row}~{minute}` - the routines lane's tick-scoped
    grammar. DBOS dedupes on the id forever, and the studio jobs swallow their failures, so a
    row-only id made Test/Draft single-shot per version (found in review); within a minute a
    double click still dedupes."""
    return f"{kind}-{row_id}~{utcnow():%Y%m%dT%H%M}"


async def request_test(
    session: AsyncSession,
    *,
    actor: Actor,
    name: str,
    project_id: uuid.UUID | None = None,
) -> str:
    """Enqueue the selection eval on the worker (the web can finally test). Same
    contract as the CLI's in-process `bot_selection.test_selection` + `apply_eval`, same verdict columns -
    the gate keeps meaning what it meant, only the seat running it moved."""
    await authorize(session, actor, project_id, min_role=Role.EDITOR)
    await allowance.require(session, tenant.workspace_of(session), actor=actor)  # the job spends
    record = await working_record(session, actor, name, project_id=project_id)
    # The presence checks fire HERE, not on the worker: an enqueue that will certainly fail is
    # a 422 the author never sees (the job's contract swallows into a log). Same messages as
    # the in-process test path, so the CLI and the web teach identically.
    require_scorable(name, record.manifest or {})
    if record.status != BotStatus.DRAFT.value:
        # The verdict lands on a DRAFT only (`apply_eval`): a paid eval of a published head
        # would be scored and thrown away (found in review).
        raise Conflict(
            f"{name} has no draft to test - v{record.version} is published and its verdict is frozen"
        )
    key = _studio_job_key("bottest", record.id)
    await dispatch.get_dispatcher().enqueue(
        "test_bot",
        key,
        actor_id=str(actor.id),
        name=name,
        project_id=str(project_id) if project_id else None,
        version=record.version,
        workspace_id=str(record.workspace_id),  # the job acts for the bot's workspace
    )
    return key


async def routines_of(
    session: AsyncSession,
    *,
    actor: Actor,
    name: str,
    project_id: uuid.UUID | None = None,
) -> list[Routine]:
    """The bot's standing work - the routines whose payload names this bot."""
    await authorize(session, actor, project_id, min_role=Role.VIEWER)
    return await Routine.for_keeper(session, actor, bot_name=name, project_id=project_id)


async def stats_of(
    session: AsyncSession,
    *,
    actor: Actor,
    name: str,
    project_id: uuid.UUID | None = None,
) -> BotStats:
    """The stats strip: spend and runs by join (creator-scoped - spend belongs to whoever
    paid), plus the memory and routine counts the page composes."""
    await authorize(session, actor, project_id, min_role=Role.VIEWER)
    total, month, runs, last = await PlanStep.spend_of_agent(session, name, creator_actor_id=actor.id)
    carrier: dict[str, object] = {"project_id": project_id} if project_id else {"user_id": actor.id}
    memory_count = await Memory.active_count(
        session,
        MemoryScope.BOT,
        bot_name=name,
        **carrier,  # type: ignore[arg-type]
    )
    routines = await Routine.for_keeper(session, actor, bot_name=name, project_id=project_id)
    return BotStats(
        spend_usd_total=total,
        spend_usd_30d=month,
        runs=runs,
        last_run_at=last,
        memory_count=memory_count,
        routine_count=len(routines),
    )


# ── the messenger: the pin, the per-bot shelves ────────────────────────────
# The DM lane (`dm:{name}` task threads, `chat_task`/`send_chat`) is REMOVED, replaced by
# Conversations (`operations/chat.py`): talk was implemented as work, and a one-line
# question planned a step, booted a frame, published, and signed a receipt. A conversation
# is one frameless model call per reply; work mints an ordinary Task through the door.


async def set_pinned(
    session: AsyncSession, actor: Actor, name: str, *, pinned: bool, project_id: uuid.UUID | None = None
) -> BotRecord:
    """The sidebar law: a bot appears in the left nav ONLY while pinned. Per-bot like the face -
    every version of the name wears the same pin, so a draft ahead never disagrees with the head."""
    await authorize(session, actor, project_id, min_role=Role.EDITOR)
    rows = await BotRecord.all_versions(session, name, project_id=project_id, owner_actor_id=actor.id)
    if not rows:
        raise NotFound(f"no bot named {name!r} in this scope")
    for row in rows:
        row.pinned = pinned
    await session.commit()
    return rows[0]


async def runs_of(
    session: AsyncSession, actor: Actor, name: str, *, limit: int = 30
) -> list[dict[str, object]]:
    """The Runs tab's feed: everywhere this bot served a step - DM turns and One-routed tasks
    alike - with outcome and metered spend. Values, never rows (Rule 6 has cousins everywhere)."""
    runs = await PlanStep.runs_of_agent(session, name, creator_actor_id=actor.id, limit=limit)
    return [
        {
            "task_id": str(task_id),
            "goal": goal,
            "title": step.title,
            "state": step.state,
            "attempt": step.attempt,
            "cost_usd": f"{usd:.2f}",
            "at": step.created_at.isoformat(),
        }
        for step, task_id, goal, usd in runs
    ]


async def artifacts_of(
    session: AsyncSession, actor: Actor, name: str, *, limit: int = 30
) -> list[dict[str, object]]:
    """The Artifacts tab's shelf: everything this bot published, chat and routed tasks alike,
    each with its verification verdict."""
    rows = await Artifact.by_agent(session, name, creator_actor_id=actor.id, limit=limit)
    out: list[dict[str, object]] = []
    for artifact, goal in rows:
        verification = await Verification.latest_for(session, artifact.id)
        out.append(
            {
                "id": str(artifact.id),
                "task_id": str(artifact.task_id),
                "title": artifact.title,
                "filename": str(artifact.provenance.get("path", artifact.title)).rsplit("/", 1)[-1],
                "type": str(artifact.type),
                "verdict": verification.verdict if verification is not None else None,
                "goal": goal,
                "at": artifact.created_at.isoformat(),
            }
        )
    return out
