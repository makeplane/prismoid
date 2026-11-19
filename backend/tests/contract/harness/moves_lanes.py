# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What happens around a first turn, as moves a scenario lists in `then` (and rows it lists in
`given`): a follow-up, a published bot, a judge's seat, a fleet's seats, the receipts the queue was handed.

A move drives the engine through the same public verbs the doors call and never decides what the
record should look like; what it wants held beside the record it puts in `run.facts`.
"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.verification.pipeline import Check, verdict_of
from prismoid.db.models.actor import Actor
from prismoid.db.models.feature import Feature
from prismoid.db.models.verification import Verification
from prismoid.jobs.verify import verify_artifact
from prismoid.operations import bots as bot_ops
from prismoid.operations import features
from prismoid.operations import models as model_ops
from prismoid.operations import projects as project_ops
from prismoid.operations import tasks as task_ops
from prismoid.schemas.bots import BotSource, SelectionCaseResult, SelectionReport
from tests.contract.harness.rig import Move, Run, Setup
from tests.contract.harness.shape import UUID

BOT_MD = """---
name: {name}
description: >
  Recaps a meeting from its notes. Load when: notes need a one-page recap. Not for: minutes.
capability: frontier-reasoner
produces: document
tools: [frame.write_file, frame.publish]
---
Write the recap: decisions first, then who owes what.
"""


def published_bot(name: str) -> Setup:
    """A bot its author has tested and published, ready to be pinned as the one line of a task."""

    async def setup(session: AsyncSession, actor: Actor) -> None:
        goal = "Recap this meeting"
        source = BotSource(
            bot_md=BOT_MD.format(name=name), cases_yaml=f"selection:\n  - goal: {goal}\n    expect: {name}\n"
        )
        row = await bot_ops.push(session, actor=actor, name=name, source=source)
        case = SelectionCaseResult(goal=goal, expected=name, chosen=name, ok=True)
        report = SelectionReport(bot=name, version=row.version, passed=1, total=1, cases=(case,))
        await bot_ops.apply_eval(session, actor=actor, name=name, version=row.version, report=report)
        await bot_ops.publish(session, actor=actor, name=name)

    return setup


async def judge_seat(session: AsyncSession, actor: Actor) -> None:
    """A judge on a keyless local server, so any host can build it; its id is this run's own."""
    await model_ops.add_model(
        session,
        provider="local",
        model_id=f"contract-judge-{uuid.uuid4().hex[:8]}",
        family="local",
        protocol="openai_compatible",
        classes=["judge"],
        base_url="http://judge.contract.invalid/v1",
        enabled=True,
    )


CHEAP = Decimal("0.000001")  # USD a token: a seat's writer run costs a few cents
DEAR = Decimal(1)  # USD a token: no budget a scenario sets buys this seat


def fleet_of(cheap: int, dear: int = 0) -> Setup:
    """A Project with fleet mode on and its seats: `cheap` writer-class rows, then `dear` frontier rows (after
    them in catalog order, so a budget meets them last), each with an id of its own; the door gets their refs."""

    async def setup(session: AsyncSession, actor: Actor) -> Mapping[str, object]:
        project = (await project_ops.create(session, actor, name="fleet")).project
        session.add(Feature(name=features.FLEET, project_id=project.id, enabled=True))
        await session.flush()
        refs: list[str] = []
        seats = [("a", "fast-executor", CHEAP)] * cheap + [("b", "frontier-reasoner", DEAR)] * dear
        for order, model_class, usd in seats:
            row = await model_ops.add_model(
                session,
                provider="local",
                model_id=f"contract-{order}-{uuid.uuid4().hex[:8]}",
                family="local",
                protocol="openai_compatible",
                classes=[model_class],
                base_url="http://seat.contract.invalid/v1",
                prices={"input_token": usd, "output_token": usd},
                enabled=True,
            )
            refs.append(f"{row.provider}:{row.model_id}")
        return {"project_id": project.id, "models": refs}

    return setup


def follow_up(goal: str) -> Move:
    """The person sends another message on the settled task: the next turn of the same task."""

    async def move(run: Run) -> None:
        async with run.acting() as (session, actor):
            task = await task_ops.continue_task(session, actor, run.task_id, goal=goal, clarify="never")
            turn = task.turn
        await run.play(turn)

    return move


def receipts() -> Move:
    """Start the verify jobs the queue was handed after delivery; hold each one's word and its suite's
    own verdict and checks, apart from the judge's call."""

    async def move(run: Run) -> None:
        held: list[dict[str, Any]] = []
        for queued in [q for q in run.cast.enqueued if q.job == "verify_artifact" and not q.played]:
            queued.played = True
            word = await inspect.unwrap(verify_artifact)(**queued.args)
            async with run.acting() as (session, _):
                row = await Verification.latest_for(session, uuid.UUID(str(queued.args["artifact_id"])))
                assert row is not None, "a receipt the job signed has no row"
                checks = [Check.model_validate(c) for c in row.report["checks"]]
            suite = {
                "suite_verdict": str(verdict_of(checks)),
                "checks": [[c.name, str(c.status)] for c in checks],
            }
            held.append({"job": word, **suite})
        run.facts["receipts"] = held

    return move


def jobs(*names: str) -> Move:
    """Hold the jobs of these kinds the API side started, in order, with ids made nameless."""

    async def move(run: Run) -> None:
        subject = str(run.task_id)
        run.facts["jobs"] = [
            f"{q.job} {UUID.sub('{id}', q.key.replace(subject, '{task}'))}"
            for q in run.cast.enqueued
            if q.job in names
        ]

    return move
