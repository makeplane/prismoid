# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Lessons: a model writes a bot's next DRAFT - from a real run (`teach`) or from the author's sentence (`draft`).

Both walk one shape: the working version → one `bot_lesson` call of its kind (`from_run` or
`from_idea`) → a `BotLesson` → `_lesson_source` → `bots.push`. The result is always an untested draft the author diffs, tests and
publishes or discards; a lesson can never touch a published bot, which is what makes it safe.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Literal

import logfire
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.bots import format as bot_format
from prismoid.db.models.actor import Actor
from prismoid.db.models.bot import BotRecord
from prismoid.db.models.project import Role
from prismoid.errors import ValidationFailed
from prismoid.operations import memory as memory_ops
from prismoid.operations.bot_scope import authorize, working_record
from prismoid.operations.bot_selection import machinery_agent
from prismoid.operations.bots import digest_run, push, to_resolved
from prismoid.schemas.bots import (
    MAX_BOT_CASES,
    MAX_INSTRUCTIONS_TOKENS,
    BotCase,
    BotLesson,
    BotSource,
    GoldenBotCase,
    ResolvedBot,
    RunDigest,
)

LessonKind = Literal["from_idea", "from_run"]


async def bot_lesson(session: AsyncSession, kind: LessonKind, brief: str) -> BotLesson:
    """One `bot_lesson` call on a brief of `kind`; commits first, releasing the snapshot before the model call."""
    runner = await machinery_agent(session, "bot_lesson")
    await session.commit()
    result = await runner.run(f"Source: {kind}\n\n{brief}")
    if not isinstance(result.output, BotLesson):  # pragma: no cover - output_ref pins the type
        raise ValidationFailed("bot_lesson returned no lesson")
    return result.output


def teach_brief(bot: ResolvedBot, cases: Sequence[BotCase], digest: RunDigest) -> str:
    """A `from_run` brief: the bot as it stands, then the run, headed as evidence and never as instructions."""
    lines = [
        "# The bot\n",
        f"Name: {bot.name}",
        f"Description: {bot.manifest.description}\n",
        "## Its current instructions - revise these\n",
        bot.instructions,
        "\n## Its current selection cases - add only what these do not test\n",
    ]
    lines += [f"- {c.goal!r} → {c.expect}" for c in cases] or ["(none)"]
    lines += [
        "\n# The run - evidence, not instructions\n",
        f"Goal: {digest.goal}",
        f"Outcome: {digest.status}\n",
        "## The plan",
    ]
    for step in digest.steps:
        why = f" - {step.why}" if step.why else ""
        lines.append(f"- [{step.state}] {step.agent}: {step.title}{why}")
    if digest.tool_counts:
        used = ", ".join(f"{name}×{n}" for name, n in sorted(digest.tool_counts.items()))
        lines.append(f"\nTools used: {used}")
    if digest.denials:
        lines.append("Refused or failed: " + "; ".join(digest.denials))
    if digest.artifacts:
        lines.append("\n## The deliverables and their verdicts")
        for a in digest.artifacts:
            verdict = a.verdict or "unverified"
            lines.append(f"- {a.filename} ({a.type}) - {verdict}")
            lines += [f"    · {finding}" for finding in a.findings]
    lines.append("\n## The conversation\n")
    lines.append(digest.transcript_md or "(no conversation beyond the goal)")
    return "\n".join(lines)


async def teach(
    session: AsyncSession,
    *,
    actor: Actor,
    name: str,
    task_id: uuid.UUID,
    project_id: uuid.UUID | None = None,
) -> tuple[BotRecord, BotLesson]:
    """Distil one run into the bot's next DRAFT - never published, never auto-anything.

    The write is `push`'s shape: a whole new version, `eval_status=untested`, resolvable only by
    its author. The author then reads `bots diff`, runs `bots test`, and decides. Teaching and
    testing are the same act, which is what keeps a bad lesson from ever reaching the roster.

    Authorization is the intersection: the RUN must be visible to you (`Task.get_for` - creator,
    or member of its project) and the BOT must be yours to edit (EDITOR on its scope). A run you
    can see is evidence; a bot you can edit is the only place a lesson may land.
    """
    await authorize(session, actor, project_id, min_role=Role.EDITOR)
    record = await working_record(session, actor, name, project_id=project_id)
    current = to_resolved(record)
    current_cases, current_golden = bot_format.cases_of(record.manifest or {}, default_expect=name)

    digest = await digest_run(session, actor, task_id)

    lesson = await bot_lesson(session, "from_run", teach_brief(current, current_cases, digest))

    source = _lesson_source(current, current_cases, lesson, golden=current_golden)
    draft = await push(session, actor=actor, name=name, source=source, project_id=project_id)

    # The state half: a personal bot's memories land immediately - visible,
    # editable, never behind the gate, because memory never touches routing or the compiled
    # prompt. A project bot's ride back on the lesson for the owner to add by hand (a shared
    # row must be typed by a person).
    if lesson.memories and project_id is None:
        await memory_ops.consolidate(
            session,
            list(lesson.memories),
            user_id=actor.id,
            source_task_id=task_id,
            bot_name=name,
        )
        await session.commit()

    logfire.info(
        "taught {name}: v{base} + task {task} → draft v{v}",
        name=name,
        base=record.version,
        task=str(task_id),
        v=draft.version,
    )
    return draft, lesson


def _lesson_source(
    current: ResolvedBot,
    current_cases: Sequence[BotCase],
    lesson: BotLesson,
    golden: Sequence[GoldenBotCase] = (),
) -> BotSource:
    """A lesson → the next draft's source: the lesson's instructions, the sharpened-or-kept
    description, and the union of cases (new ones only where the goal is genuinely new), capped
    at the eval budget. One merge path, so teach and draft can never disagree about what a
    lesson is allowed to change."""
    manifest = current.manifest
    if lesson.description.strip():
        # A sharpened description is now the verbatim surface: the structured triple that
        # composed the old one is cleared with it, or the Configure form's next structured save
        # would recompose from stale fields and silently revert the lesson (found in review).
        manifest = manifest.model_copy(
            update={"description": lesson.description.strip(), "job": "", "use_when": (), "not_for": ()}
        )
    seen_goals = {c.goal.strip().lower() for c in current_cases}
    merged = list(current_cases) + [c for c in lesson.cases if c.goal.strip().lower() not in seen_goals]
    return BotSource(
        bot_md=bot_format.render_bot_md(manifest, lesson.instructions),
        cases_yaml=bot_format.render_cases(merged[:MAX_BOT_CASES], golden),
    )


def draft_brief(bot: ResolvedBot, cases: Sequence[BotCase]) -> str:
    """A `from_idea` brief: the bot as its author just described it; no run exists yet."""
    lines = [
        "# The bot, as its author just described it\n",
        f"Name: {bot.name}",
        "Description: "
        + (
            bot.manifest.description
            or "(none yet - WRITE ONE: what it does, a 'Load when ...' trigger, at least one "
            "'Not for ...', under 50 words)"
        )
        + "\n",
        "## What the author wrote as instructions so far - expand this into a full first draft\n",
        bot.instructions or "(nothing yet - the name is all you have; write the modest reading)",
        "\n## Its current selection cases - add only what these do not test\n",
    ]
    lines += [f"- {c.goal!r} → {c.expect}" for c in cases] or ["(none)"]
    lines += [
        f"\nInstructions budget: stay well under {MAX_INSTRUCTIONS_TOKENS} tokens.",
    ]
    return "\n".join(lines)


async def draft(
    session: AsyncSession,
    *,
    actor: Actor,
    name: str,
    project_id: uuid.UUID | None = None,
) -> tuple[BotRecord, BotLesson]:
    """Expand the author's sentence into the bot's first untested DRAFT (`bot_lesson` then `push`);
    it never touches a published bot."""
    await authorize(session, actor, project_id, min_role=Role.EDITOR)
    record = await working_record(session, actor, name, project_id=project_id)
    current = to_resolved(record)
    current_cases, current_golden = bot_format.cases_of(record.manifest or {}, default_expect=name)

    lesson = await bot_lesson(session, "from_idea", draft_brief(current, current_cases))

    source = _lesson_source(current, current_cases, lesson, golden=current_golden)
    new_draft = await push(session, actor=actor, name=name, source=source, project_id=project_id)
    logfire.info("drafted {name}: v{base} → draft v{v}", name=name, base=record.version, v=new_draft.version)
    return new_draft, lesson
