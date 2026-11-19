# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The receipt's judge reads the pages the work read, under the source ids it cites them by.

A fleet seat or a researcher declares no source file: what it read is the pages the task kept. A judge
told "the author was given nothing" fails every cited claim on that alone. The suite and the provider
round trip are stubbed; the task, its kept pages and the receipt are real. The stub meters two calls at
once, as a judge's tools do: a model call that metered on the caller's session would share its connection.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from prismoid.ai.agents.run import AgentRunner
from prismoid.ai.gateway import providers
from prismoid.ai.verification import pipeline
from prismoid.db.models.artifact import Artifact
from prismoid.db.models.usage import UsageEvent
from prismoid.db.session import session_scope
from prismoid.operations import models as model_ops
from prismoid.operations import routing as routing_ops
from prismoid.operations import sources as sources_ops
from prismoid.operations import tasks as task_ops
from prismoid.operations import verification
from prismoid.operations.execution import finish
from prismoid.schemas.model_class import ModelClass
from prismoid.schemas.usage import UsageRecord
from prismoid.schemas.verification import JudgeVerdict, Verdict
from tests.support.artifacts import deliverable, verified
from tests.support.cast import member

pytestmark = [pytest.mark.integration]

PAGE = "Kochi had a population of 602,046 at the 2011 census."
URL = "https://en.wikipedia.org/wiki/Kochi"
ANSWER = b"# Kochi\n\nKochi had 602,046 people in 2011 [S1].\n"


@pytest.fixture
async def asked(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """The prompts a judge of another family is asked; the suite passes and leaves no extracted text."""
    prompts: list[Any] = []

    async def run(runner: AgentRunner, prompt: Any) -> Any:
        deps: Any = runner.deps
        agent: Any = runner.agent
        spec = agent.model.wrapped.spec
        prompts.append(prompt)
        record = UsageRecord(
            task_id=deps.task_id,
            provider=spec.provider,
            model=spec.model_id,
            family=spec.family,
            units={},
            cost_usd=Decimal(0),
            price_version=1,
            at=dt.datetime.now(dt.UTC),
        )
        await asyncio.gather(deps.emit_usage(record), deps.emit_usage(record))
        return SimpleNamespace(output=JudgeVerdict(verdict=Verdict.PASS, rationale="every figure is on S1"))

    async def suite(**_kwargs: Any) -> verification.SuiteRun:
        return verification.SuiteRun(
            [
                pipeline.Check(
                    name="bytes:sha256", status=pipeline.CheckStatus.PASS, kind=pipeline.CheckKind.STRUCTURAL
                )
            ]
        )

    model_id = f"judge-{uuid.uuid4().hex[:8]}"

    async def order_for(_session: Any) -> dict[str, list[str]]:
        return {ModelClass.JUDGE.value: [f"google:{model_id}"]}

    every_row = model_ops.routed_catalog

    async def only_this_judge(session: Any) -> Any:
        rows, order = await every_row(session)
        return [row for row in rows if row.model_id == model_id], order

    monkeypatch.setattr(providers, "api_key_for", lambda _provider: "a-key")
    monkeypatch.setattr(AgentRunner, "run", run)
    monkeypatch.setattr(routing_ops, "order_for", order_for)
    monkeypatch.setattr(model_ops, "routed_catalog", only_this_judge)
    monkeypatch.setattr(verification, "run_in_a_fresh_frame", suite)
    async with session_scope() as s:
        await model_ops.add_model(
            s,
            provider="google",
            model_id=model_id,
            family="google",
            protocol="google",
            classes=["judge"],
            enabled=True,
        )
    return prompts


async def test_a_seat_answer_is_judged_against_the_pages_it_read(
    silent_dispatcher: None, asked: list[Any]
) -> None:
    async with session_scope() as s:
        actor, workspace = await member(s, "Ada")
        task = await task_ops.create_task(s, actor, goal="Kochi's population")
        artifact = deliverable(task.id, ANSWER, path="/artifacts/kochi.md", title="Kochi")
        artifact.provenance = {**artifact.provenance, "sources": ["/task/context.md"]}
        s.add(artifact)
        await s.commit()
    keep = sources_ops.keeper(task.id)
    assert await keep.cite(URL, await keep(PAGE)) == "S1"

    signed = await verified(artifact.id, workspace.id)

    assert len(asked) == 1
    prompt = str(asked[0])
    assert f"[S1] {URL}" in prompt and PAGE in prompt, "the page reads under the id the answer cites"
    assert "the author was given nothing" not in prompt
    assert prompt.index(PAGE) < prompt.index("## The deliverable"), "the sources come before the work"
    assert signed.report.judge is not None and signed.report.judge.verdict is Verdict.PASS


async def test_the_in_line_receipt_meters_off_the_steps_session(
    silent_dispatcher: None, asked: list[Any]
) -> None:
    """The fleet verifies in line on the step's session; the judge's spend lands on its own transactions."""
    async with session_scope() as s:
        actor, workspace = await member(s, "Ada")
        task = await task_ops.create_task(s, actor, goal="Kochi's population")
        artifact = deliverable(task.id, ANSWER, path="/artifacts/kochi.md", title="Kochi")
        s.add(artifact)
        await s.commit()
    keep = sources_ops.keeper(task.id)
    await keep.cite(URL, await keep(PAGE))

    async with session_scope(workspace_id=workspace.id) as s:
        row = await s.get(Artifact, artifact.id)
        assert row is not None
        assert (await finish.verify(s, [row])).findings == []

    async with session_scope(workspace_id=workspace.id) as s:
        assert len(await UsageEvent.for_task(s, task.id)) == 2, "the judge's spend is on the ledger"


async def test_a_receipt_no_judge_answered_is_marked_unchecked(
    silent_dispatcher: None, asked: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A seat whose judge never answered is not failed, but its review says it is unchecked."""

    async def silent(_runner: AgentRunner, _prompt: Any) -> Any:
        raise TimeoutError("no answer")

    monkeypatch.setattr(AgentRunner, "run", silent)
    async with session_scope() as s:
        actor, workspace = await member(s, "Ada")
        task = await task_ops.create_task(s, actor, goal="Kochi's population")
        artifact = deliverable(task.id, ANSWER, path="/artifacts/kochi.md", title="Kochi")
        s.add(artifact)
        await s.commit()

    async with session_scope(workspace_id=workspace.id) as s:
        row = await s.get(Artifact, artifact.id)
        assert row is not None
        receipts = await finish.verify(s, [row])
    assert receipts.findings == [] and receipts.unchecked
