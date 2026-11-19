# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A re-run generation never repeats a world or record write (ADR-089): a write an earlier generation
of the step finished is answered from its recorded result, marked `ALREADY_DONE`, and not made again;
a read runs again, and a write under other args acts. Each generation is a real agent run on the real
`PgCallLog`, the first killed before its last request; the next opens as a fence (another log version)
or as a fresh attempt (a ladder rung, a steer's re-run)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic_ai import Agent, DeferredToolRequests
from pydantic_ai.toolsets import FunctionToolset

from prismoid.ai.agents.run import AgentRunner
from prismoid.ai.capabilities.durability import LOG_VERSION, CallLogScope, PrismoidDurability
from prismoid.ai.capabilities.tool_record import ALREADY_DONE, ToolRecorder
from prismoid.ai.deps import TaskDeps
from prismoid.db.models.plan import PlanStep
from prismoid.db.session import session_scope
from prismoid.operations import tasks as task_ops
from prismoid.operations.call_log import PgCallLog
from prismoid.schemas.ids import StepId
from prismoid.schemas.plan import StepState, TaskStatus
from tests.support.cast import member
from tests.support.rigs.durability_drill import Killed, World
from tests.support.runners import PLAIN

pytestmark = pytest.mark.integration

POST = ("slack_post", {"channel": "launch", "text": "we ship friday"})  # a connector write: WORLD
READ = ("read_file", {"path": "/work/notes.md"})  # a catalog read


async def _journal(*_: Any, **__: Any) -> None:
    return None


@pytest.fixture
async def step(silent_dispatcher: object) -> AsyncIterator[tuple[StepId, PgCallLog]]:
    """A running step and its workspace's call log."""
    async with session_scope() as s:
        ada, workspace = await member(s, "Ada")
        task = await task_ops.create_task(s, ada, goal="announce the launch", clarify="never")
        task.transition(TaskStatus.PLANNING)
        task.transition(TaskStatus.RUNNING)
        row = PlanStep(task_id=task.id, ordinal=0, title="post it", state=StepState.RUNNING.value)
        s.add(row)
        await s.commit()
        yield StepId(row.id), PgCallLog(workspace.id)


def _toolset(acted: list[str]) -> FunctionToolset[Any]:
    """The remote side: every call that reaches it is one line in `acted`."""
    toolset = FunctionToolset[Any](id="probe")

    def slack_post(channel: str, text: str) -> str:
        """Post a message to a channel."""
        acted.append(f"post {text}")
        return f"posted to #{channel}: ts=1731600000.{len(acted)}"

    def read_file(path: str) -> str:
        """Read a file."""
        acted.append(f"read {path}")
        return "notes"

    def crm_update_deal(deal: str, stage: str) -> str:
        """Move a deal to a stage."""
        acted.append(f"update {deal}")
        return f"{deal} is now {stage}"

    toolset.add_function(slack_post)
    toolset.add_function(read_file)
    # A connector write its server mislabels as read-only.
    toolset.add_function(crm_update_deal, metadata={"annotations": {"readOnlyHint": True}})
    return toolset


async def _generation(
    world: World, acted: list[str], log: PgCallLog, step: StepId, *, fresh: bool, version: int = LOG_VERSION
) -> Any:
    """One step attempt on its call log, the way `build()` mounts the record and the log."""
    agent = Agent(
        world.model(),
        name="probe",
        deps_type=TaskDeps,
        toolsets=[_toolset(acted)],
        output_type=[str, DeferredToolRequests],
        capabilities=[ToolRecorder(), PrismoidDurability()],
    )
    scope = await CallLogScope.open(log, step, fresh=fresh, version=version)
    runner = AgentRunner(agent, TaskDeps(step_id=step, journal_tool=_journal), PLAIN)
    return await runner.run("announce the launch", log=scope)


def _returns(result: Any, tool: str) -> list[str]:
    return [
        str(p.content)
        for m in result.all_messages()
        for p in m.parts
        if p.part_kind == "tool-return" and p.tool_name == tool
    ]


async def _first_generation(acted: list[str], log: PgCallLog, step: StepId) -> None:
    """Generation 0 posts and reads, then dies before it answers. The two calls of one response run
    concurrently, so they reach the remote in either order."""
    world = World([[POST, READ]], die_before=2)
    with pytest.raises(Killed):
        await _generation(world, acted, log, step, fresh=False)
    assert sorted(acted) == ["post we ship friday", "read /work/notes.md"]


@pytest.mark.parametrize("fresh", [False, True], ids=["fence", "fresh-attempt"])
async def test_a_write_an_earlier_generation_finished_is_never_made_again(
    step: tuple[StepId, PgCallLog], fresh: bool
) -> None:
    """The same write is one remote call, and the model is handed its recorded result; the read re-runs."""
    step_id, log = step
    acted: list[str] = []
    await _first_generation(acted, log, step_id)

    result = await _generation(
        World([[POST, READ]]), acted, log, step_id, fresh=fresh, version=LOG_VERSION + (not fresh)
    )

    assert acted.count("post we ship friday") == 1, "the write reached the remote once"
    assert acted.count("read /work/notes.md") == 2, "the read ran again"
    (posted,) = _returns(result, "slack_post")
    first_ts = f"posted to #launch: ts=1731600000.{acted.index('post we ship friday') + 1}"
    assert posted.startswith(ALREADY_DONE) and first_ts in posted, "the model got generation 0's result"
    latest = await log.latest(step_id)
    assert latest.key is not None and latest.key.generation == 1


async def test_a_third_generation_still_finds_the_first_ones_write(step: tuple[StepId, PgCallLog]) -> None:
    """Every lower generation is read from the store, not only the one just below."""
    step_id, log = step
    acted: list[str] = []
    await _first_generation(acted, log, step_id)
    await _generation(World([[POST]]), acted, log, step_id, fresh=True)

    result = await _generation(World([[POST]]), acted, log, step_id, fresh=True)

    assert acted.count("post we ship friday") == 1
    (posted,) = _returns(result, "slack_post")
    assert posted.startswith(ALREADY_DONE)


async def test_a_write_under_other_args_acts(step: tuple[StepId, PgCallLog]) -> None:
    """Another digest is another call: it reaches the remote."""
    step_id, log = step
    acted: list[str] = []
    await _first_generation(acted, log, step_id)
    other = ("slack_post", {"channel": "launch", "text": "we ship monday"})

    result = await _generation(World([[other]]), acted, log, step_id, fresh=True)

    assert acted.count("post we ship monday") == 1
    (posted,) = _returns(result, "slack_post")
    assert not posted.startswith(ALREADY_DONE)


async def test_a_crash_replay_of_the_answered_generation_answers_it_the_same(
    step: tuple[StepId, PgCallLog],
) -> None:
    """A generation that answered from the record and then died replays to the same answer: no call."""
    step_id, log = step
    acted: list[str] = []
    await _first_generation(acted, log, step_id)
    world = World([[POST]], die_before=2)
    with pytest.raises(Killed):
        await _generation(world, acted, log, step_id, fresh=True)
    world.die_before = None

    result = await _generation(world, acted, log, step_id, fresh=False)

    assert acted.count("post we ship friday") == 1
    assert world.provider_calls == 2, (
        "the re-entered generation replayed its first request, bought again nothing"
    )
    (posted,) = _returns(result, "slack_post")
    assert posted.startswith(ALREADY_DONE)


async def test_a_connector_write_labelled_read_only_is_still_made_once(
    step: tuple[StepId, PgCallLog],
) -> None:
    """A connector's tool counts as acting whatever its server's hints say."""
    step_id, log = step
    acted: list[str] = []
    update = ("crm_update_deal", {"deal": "acme", "stage": "won"})
    with pytest.raises(Killed):
        await _generation(World([[update]], die_before=2), acted, log, step_id, fresh=False)

    result = await _generation(World([[update]]), acted, log, step_id, fresh=True)

    assert acted == ["update acme"], "the mislabelled write reached the remote once"
    (updated,) = _returns(result, "crm_update_deal")
    assert updated.startswith(ALREADY_DONE) and "acme is now won" in updated
