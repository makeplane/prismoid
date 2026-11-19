# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`draft_bot` + `test_bot` - the authoring model calls, moved onto the worker (ADR-056).

Rule 5 keeps model calls out of the API process; until now that meant bot drafting and the
selection eval were CLI-only, and the web's create dialog dead-ended at "now go run a
terminal command". These two jobs are the same operations the CLI runs in-process
(`operations/bots.draft`, `test_selection` + `apply_eval`), executed on the worker's own
modest queue so authoring can never starve the step lane.

The contract is `verify_artifact`'s: **a failure costs the draft, never anything else.**
Every exception is swallowed into a log line; the caller polls the bot itself (a new version
appears, or `eval_status` changes) and the web's dialog gives up honestly after its wait.
Tick-scoped ids (`botdraft-{row_id}~{minute}` / `bottest-{row_id}~{minute}`) make a double click
a dedupe and a re-run a minute later a real run. Each job carries the bot row's `workspace_id` and
opens its unit of work bound to it.
"""

from __future__ import annotations

import uuid

import logfire

from prismoid.jobs import durable


@durable.effect()
async def _draft(actor_id: str, name: str, project_id: str | None, workspace_id: str) -> str:
    from prismoid.db.models.actor import Actor
    from prismoid.db.session import session_scope
    from prismoid.operations import bot_lessons

    try:
        async with session_scope(_workspace(workspace_id)) as session:
            actor = await session.get(Actor, uuid.UUID(actor_id))
            if actor is None or not actor.is_active():
                return "gone"  # suspended or erased between click and run - nothing to author
            record, _lesson = await bot_lessons.draft(
                session, actor=actor, name=name, project_id=uuid.UUID(project_id) if project_id else None
            )
            return f"draft v{record.version}"
    except Exception:
        logfire.exception("drafting {name} failed; no draft was written", name=name)
        return "unrun"


@durable.effect()
async def _test(
    actor_id: str, name: str, project_id: str | None, version: int | None, workspace_id: str
) -> str:
    from prismoid.db.models.actor import Actor
    from prismoid.db.session import session_scope
    from prismoid.operations import bot_selection, bots

    try:
        async with session_scope(_workspace(workspace_id)) as session:
            actor = await session.get(Actor, uuid.UUID(actor_id))
            if actor is None or not actor.is_active():
                return "gone"
            scope = uuid.UUID(project_id) if project_id else None
            # THE version the door presence-checked (found in review: the worker scored
            # `latest_draft or head` on its own, so an abandoned older draft, or the head, got
            # the verdict the web was polling the working version for)
            report = await bot_selection.test_selection(
                session, actor, name, version=version, project_id=scope
            )
            await bots.apply_eval(
                session, actor=actor, name=name, version=report.version, report=report, project_id=scope
            )
            return "green" if report.green else f"red {report.passed}/{report.total}"
    except Exception:
        logfire.exception("selection eval for {name} failed; the verdict stays untested", name=name)
        return "unrun"


@durable.job("draft_bot")
async def draft_bot(actor_id: str, name: str, project_id: str | None = None, *, workspace_id: str) -> str:
    """One author sentence → one new DRAFT version, via `bot_lesson` from an idea."""
    return await _draft(actor_id, name, project_id, workspace_id)


@durable.job("test_bot")
async def test_bot(
    actor_id: str,
    name: str,
    project_id: str | None = None,
    version: int | None = None,
    *,
    workspace_id: str,
) -> str:
    """One working version → the selection eval → `eval_status` + `eval_report` on the row."""
    return await _test(actor_id, name, project_id, version, workspace_id)


def _workspace(workspace_id: str) -> uuid.UUID:
    """The bot's workspace, carried by the enqueue: the studio acts where the bot
    lives - the job knows the bot only by name, and a name means something only inside a workspace."""
    return uuid.UUID(workspace_id)
