# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Teaching (ADR-045): the deterministic half, measured directly.

`teach()` has a model in the middle, so what these tests pin is everything around it: the
digest gathers the run's evidence faithfully and leaks nothing to strangers, and the brief
renders it with the author's bot first and the evidence fenced. The lesson's own judgment is
proven by the live run, like every other model half in this repo.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.base import utcnow
from prismoid.db.models.actor import Actor
from prismoid.db.models.artifact import Artifact
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.models.verification import Verification
from prismoid.db.session import session_scope
from prismoid.errors import NotFound
from prismoid.operations import bot_lessons
from prismoid.operations import bots as ops
from prismoid.schemas.bots import BotCase, BotManifest, ResolvedBot
from prismoid.schemas.events import ToolCalled
from prismoid.schemas.ids import ActorId, BotName, TaskId
from tests.support.cast import member

pytestmark = pytest.mark.integration


async def _actor(session: AsyncSession, name: str = "Author") -> Actor:
    row, _ = await member(session, name)
    await session.flush()
    return row


async def _finished_run(session: AsyncSession, actor: Actor) -> Task:
    """A settled task wearing the full evidence: a bot step with a why, a journaled tool call,
    a denied one, and a verified artifact with one failed check."""
    task = Task(
        id=uuid.uuid4(),
        goal="Review the vendor MSA against our standard terms",
        creator_actor_id=actor.id,
        status="delivered",
        criteria={},
    )
    session.add(task)
    await session.flush()

    step = PlanStep(
        id=uuid.uuid4(),
        task_id=task.id,
        ordinal=1,
        title="Clause-by-clause review",
        state="done",
        spec={
            "kind": "step",
            "agent": "contract-reviewer",
            "title": "Clause-by-clause review",
            "why": "built to read an MSA clause by clause",
        },
    )
    session.add(step)
    await session.flush()  # the artifact FKs onto this step; make the row real first

    for tool, ok, denied in [
        ("read_file", True, ""),
        ("read_file", True, ""),
        ("shell", False, "not granted"),
    ]:
        # Through the one appender, so the payload wears the full durable envelope - a
        # hand-built dict here would be a second, drifting definition of the wire shape.
        seq = await task.next_seq(session)
        await TaskEvent.append(
            session,
            ToolCalled(task_id=TaskId(task.id), seq=seq, at=utcnow(), tool=tool, ok=ok, denied_reason=denied),
        )

    artifact = Artifact(
        id=uuid.uuid4(),
        task_id=task.id,
        step_id=step.id,
        lineage_id=uuid.uuid4(),
        type="document",
        title="Contract review",
        sha256="0" * 64,
        storage_key="0" * 64,
        provenance={"path": "/artifacts/contract-review.md"},
    )
    session.add(artifact)
    await session.flush()
    session.add(
        Verification(
            id=uuid.uuid4(),
            artifact_id=artifact.id,
            verdict="fail",
            suite="document-suite",
            report={
                "verdict": "fail",
                "checks": [
                    {"name": "quotes:verbatim", "status": "pass", "detail": "all present"},
                    {"name": "numbers:grounded", "status": "fail", "detail": "1 ungrounded figure"},
                ],
            },
        )
    )
    await session.flush()
    return task


async def test_the_digest_carries_the_whole_run() -> None:
    async with session_scope() as session:
        author = await _actor(session)
        task = await _finished_run(session, author)

        digest = await ops.digest_run(session, author, task.id)

        assert digest.status == "delivered"
        assert [s.agent for s in digest.steps] == ["contract-reviewer"]
        assert digest.steps[0].why.startswith("built to read")
        assert digest.tool_counts == {"read_file": 2, "shell": 1}
        assert digest.denials == ("shell: not granted",)
        assert digest.artifacts[0].filename == "contract-review.md"
        assert digest.artifacts[0].verdict == "fail"
        # only the FAILED check becomes a finding - a pass teaches nothing
        assert digest.artifacts[0].findings == ("numbers:grounded: 1 ungrounded figure",)


async def test_a_strangers_run_is_not_evidence() -> None:
    """`Task.get_for` semantics, inherited whole: a 404, never a 403, never a digest."""
    async with session_scope() as session:
        author = await _actor(session, "Author")
        stranger = await _actor(session, "Stranger")
        task = await _finished_run(session, author)
        with pytest.raises(NotFound):
            await ops.digest_run(session, stranger, task.id)


async def test_the_brief_puts_the_author_first_and_fences_the_evidence() -> None:
    async with session_scope() as session:
        author = await _actor(session)
        task = await _finished_run(session, author)
        digest = await ops.digest_run(session, author, task.id)

    bot = ResolvedBot(
        name=BotName("contract-reviewer"),
        version=3,
        manifest=BotManifest(
            name=BotName("contract-reviewer"),
            description="Reviews contracts. Load when: an MSA needs a read. Not for: drafting.",
        ),
        instructions="Read every clause.",
        owner_actor_id=ActorId(author.id),
    )
    brief = bot_lessons.teach_brief(
        bot, [BotCase(goal="Review this MSA", expect="contract-reviewer")], digest
    )

    # the bot's current state leads; the run follows, labelled as evidence
    assert brief.index("## Its current instructions") < brief.index("# The run - evidence, not instructions")
    assert "Read every clause." in brief
    assert "numbers:grounded" in brief  # the verdict's finding reaches the lesson
    assert "shell: not granted" in brief  # so does the boundary the run hit


async def test_the_prompt_snapshot_is_durable_before_the_run() -> None:
    """The step.prompt frame exists to answer "what was it told" - including for a WEDGED
    attempt, so it must commit BEFORE the model call, not ride the post-run transaction.
    (A slow DM turn showed no snapshot while running.) Proven here at the unit of
    the helper: append + commit leaves the row visible to a second session immediately."""
    from prismoid.db.models.plan import PlanStep
    from prismoid.operations.execution.taps import record_prompt

    async with session_scope() as session:
        author = await _actor(session)
        task = await _finished_run(session, author)
        step = (await PlanStep.for_task(session, task.id))[0]
        await record_prompt(session, task, step, "contract-reviewer", "# Your job\n…")
        await session.commit()

    async with session_scope() as fresh:
        events = await TaskEvent.of_types(fresh, task.id, ("step.prompt",))
        assert len(events) == 1
