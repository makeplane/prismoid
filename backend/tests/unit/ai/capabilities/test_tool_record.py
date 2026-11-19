# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The tool record (`ToolRecorder`): one `tool.called` per call with keys never values, whatever the
call's outcome, and OUTCOME UNKNOWN for a call the call log saw start and never end. Each case runs a
scripted model on a real agent; the journal is what the test reads."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from pydantic_ai import Agent, DeferredToolRequests, ModelRetry, RunContext
from pydantic_ai.capabilities import AbstractCapability, AgentCapability
from pydantic_ai.exceptions import CallDeferred, SkipToolExecution
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import FunctionToolset

from prismoid.ai.agents.run import AgentRunner
from prismoid.ai.capabilities.durability import CallLogScope, PrismoidDurability
from prismoid.ai.capabilities.tool_record import OUTCOME_UNKNOWN, UNKNOWN_REASON, ToolRecorder
from prismoid.ai.deps import TaskDeps
from prismoid.ai.tools import catalog
from prismoid.ai.tools.call_rows import Said, journal_egress_denials, say
from prismoid.errors import ModelIncapable
from prismoid.schemas.ids import StepId
from tests.support.fakes.frame_files import Journal
from tests.support.rigs.durability_drill import Killed, MemoryLog, World
from tests.support.runners import PLAIN


def _runner(
    world: World,
    toolset: FunctionToolset[Any],
    journal: Journal,
    *inner: AgentCapability[TaskDeps],
    step: StepId | None = None,
) -> AgentRunner:
    """A runner the way `build()` mounts the record: outermost, the call log innermost."""
    agent = Agent(
        world.model(),
        name="probe",
        deps_type=TaskDeps,
        toolsets=[toolset],
        output_type=[str, DeferredToolRequests],
        capabilities=[ToolRecorder(), *inner, PrismoidDurability()],
    )
    return AgentRunner(agent, TaskDeps(step_id=step, journal_tool=journal), PLAIN)


async def test_a_call_nothing_described_is_one_row_named_from_the_catalog() -> None:
    """The recorder's own row: the catalog's journal name, the call's label (no query string), its id."""
    journal, toolset = Journal(), FunctionToolset[Any](id="probe")

    def web_fetch(url: str) -> str:
        """Read a page."""
        return "the page"

    toolset.add_function(web_fetch)
    world = World([[("web_fetch", {"url": "https://example.com/a?token=s3cret"})]])
    await _runner(world, toolset, journal).run("read it")
    [row] = journal.entries
    assert row["tool"] == "web.fetch" and row["ok"] is True and row["denied_reason"] == ""
    assert row["summary"] == "example.com/a"
    assert row["tool_call_id"]


async def test_a_row_said_under_the_calls_name_is_its_one_row_filled_from_the_call() -> None:
    """A tool that says its own row (a result-dependent label) replaces the recorder's: one row,
    the call's id and label where the tool left them empty."""
    journal, toolset = Journal(), FunctionToolset[Any](id="probe")

    async def shell(command: str) -> str:
        """Run a command."""
        await say(None, Said("frame.shell", False))
        return "exit 1"

    toolset.add_function(shell)
    await _runner(World([[("shell", {"command": "echo hi"})]]), toolset, journal).run("go")
    [row] = journal.entries
    assert (row["tool"], row["ok"], row["summary"]) == ("frame.shell", False, "echo hi")
    assert row["tool_call_id"]


async def test_rows_said_under_the_calls_name_many_times_are_its_one_row() -> None:
    """A call whose code says its own row several times (a search batch, one per query and engine)
    is still one row: ok when any part served, each summary named once."""
    journal, toolset = Journal(), FunctionToolset[Any](id="probe")

    async def web_search(queries: list[str]) -> str:
        """Search the web."""
        await say(None, Said.failed("web.search", LookupError("none"), "brave"))
        for _ in queries:  # identical parts, as a batch says them
            await say(None, Said("web.search", True, "openai"))
        return "results"

    toolset.add_function(web_search)
    world = World([[("web_search", {"queries": ["a", "b", "c"]})]])
    await _runner(world, toolset, journal).run("go")
    [row] = journal.entries
    assert (row["tool"], row["ok"], row["summary"]) == ("web.search", True, "brave, openai")
    assert row["denied_reason"] == "" and row["tool_call_id"]


async def test_a_row_said_under_another_name_rides_beside_the_calls_own() -> None:
    """A sub-act (a browser widen) is its own row; the call keeps its own, in the order they happened."""
    journal, toolset = Journal(), FunctionToolset[Any](id="probe")

    async def web_fetch(url: str) -> str:
        """Read a page."""
        await say(None, Said("browser.allow", True, "example.com"))
        return "the page"

    toolset.add_function(web_fetch)
    await _runner(World([[("web_fetch", {"url": "https://example.com/a"})]]), toolset, journal).run("go")
    assert [(r["tool"], r["ok"]) for r in journal.entries] == [("browser.allow", True), ("web.fetch", True)]
    assert len({r["tool_call_id"] for r in journal.entries}) == 1


@pytest.mark.parametrize("said", [False, True])
async def test_a_failure_is_one_not_ok_row_its_url_scrubbed(said: bool) -> None:
    """An exception's prose carries the URL it failed on; the record keeps host and path only,
    whether the call raised it or answered with text and said the failure itself."""
    journal, toolset = Journal(), FunctionToolset[Any](id="probe")
    failure = RuntimeError("failed for url 'https://example.com/search?q=the+users+query'")

    async def web_fetch(url: str) -> str:
        """Read a page."""
        if not said:
            raise failure
        await say(None, Said.failed("web.fetch", failure))
        return "could not read it"

    toolset.add_function(web_fetch)
    runner = _runner(World([[("web_fetch", {"url": "https://example.com/x"})]]), toolset, journal)
    if said:
        await runner.run("go")
    else:
        with pytest.raises(RuntimeError):
            await runner.run("go")
    [row] = journal.entries
    assert row["ok"] is False
    assert "example.com/search" in str(row["denied_reason"])
    assert "users+query" not in str(row["denied_reason"])


async def test_a_retry_is_a_not_ok_row_and_the_run_goes_on() -> None:
    journal, toolset, tries = Journal(), FunctionToolset[Any](id="probe"), [0]

    def look(query: str) -> str:
        """Look something up."""
        tries[0] += 1
        if tries[0] == 1:
            raise ModelRetry("try a narrower query")
        return "found"

    toolset.add_function(look)
    world = World([[("look", {"query": "everything"})], [("look", {"query": "one thing"})]])
    await _runner(world, toolset, journal).run("go")
    assert [r["ok"] for r in journal.entries] == [False, True]
    assert str(journal.entries[0]["denied_reason"]).startswith("ModelRetry: ")
    assert "try a narrower query" in str(journal.entries[0]["denied_reason"])


class _Hold(AbstractCapability[TaskDeps]):
    """A brake that holds every call, saying why when `reason` is set."""

    def __init__(self, reason: str | None) -> None:
        self.reason = reason

    async def before_tool_execute(
        self, ctx: RunContext[TaskDeps], *, call: ToolCallPart, tool_def: ToolDefinition, args: dict[str, Any]
    ) -> dict[str, Any]:
        if self.reason is not None:
            await say(
                ctx.deps.journal_tool,
                Said(
                    catalog.journal_name(call.tool_name),  # as the real holds say it (steering, the guard)
                    False,
                    denied_reason=self.reason,
                    tool_call_id=call.tool_call_id,
                ),
            )
        raise SkipToolExecution("Held, not run.")


@pytest.mark.parametrize(
    ("reason", "recorded"), [("held: Bob steered", "held: Bob steered"), (None, "Held, not run.")]
)
async def test_a_held_call_is_one_row_with_its_reason(reason: str | None, recorded: str) -> None:
    journal, world, toolset = Journal(), World([[("note", {"text": "a"})]]), FunctionToolset[Any](id="probe")

    def note(text: str) -> str:
        """Write a note."""
        world.effects.append(text)
        return "noted"

    toolset.add_function(note)
    await _runner(world, toolset, journal, _Hold(reason)).run("go")
    assert world.effects == []
    assert [(r["ok"], r["denied_reason"]) for r in journal.entries] == [(False, recorded)]


async def test_a_deferred_call_writes_no_row() -> None:
    """Its gate pair is its record."""
    journal, toolset = Journal(), FunctionToolset[Any](id="probe")

    def ask(question: str) -> str:
        """Ask the person."""
        raise CallDeferred

    toolset.add_function(ask)
    result = await _runner(World([[("ask", {"question": "which?"})]]), toolset, journal).run("go")
    assert isinstance(result.output, DeferredToolRequests)
    assert journal.entries == []


class _Ended(AbstractCapability[TaskDeps]):
    """The task ended under the run: its safe point stops the run before the call."""

    async def before_tool_execute(
        self, ctx: RunContext[TaskDeps], *, call: ToolCallPart, tool_def: ToolDefinition, args: dict[str, Any]
    ) -> dict[str, Any]:
        raise ModelIncapable("the task ended while this step ran - stopped", infra=True)


async def test_a_call_the_runs_stop_cut_off_writes_no_row() -> None:
    """Like a cancel, the stop is the run's, and the call never ran: there is no call outcome."""
    journal, world, toolset = Journal(), World([[("note", {"text": "a"})]]), FunctionToolset[Any](id="probe")

    def note(text: str) -> str:
        """Write a note."""
        world.effects.append(text)
        return "noted"

    toolset.add_function(note)
    with pytest.raises(ModelIncapable):
        await _runner(world, toolset, journal, _Ended()).run("go")
    assert world.effects == [] and journal.entries == []


def _stamps(world: World, *, die_on: str | None = None, side: str = "stamp") -> FunctionToolset[Any]:
    """A drill tool named `side`: `stamp` (no catalog row, so it may have changed the world) or a
    catalog read; it dies on `die_on` after its effect and before its result row."""
    toolset = FunctionToolset[Any](id="drill")

    def stamp(text: str) -> str:
        """Act on the world."""
        world.effects.append(text)
        if text == die_on:
            raise Killed
        return f"stamped {text}"

    toolset.add_function(stamp, name=side)
    return toolset


async def test_a_call_that_started_and_never_ended_is_unknown_once_and_never_run_again() -> None:
    """Drill D2 in process: an intent with no result is answered OUTCOME UNKNOWN and journaled once;
    a later replay hands the model the same answer and writes nothing; recorded calls never re-run."""
    step, log, journal = StepId(uuid.uuid4()), MemoryLog(), Journal()
    world = World([[("stamp", {"text": "a"})], [("stamp", {"text": "b"})], [("stamp", {"text": "c"})]])

    async def attempt(toolset: FunctionToolset[Any]) -> Any:
        scope = await CallLogScope.open(log, step, fresh=False)
        return await _runner(world, toolset, journal, step=step).run("write", log=scope)

    with pytest.raises(Killed):
        await attempt(_stamps(world, die_on="b"))
    world.die_before = 4  # the second attempt dies before its last request
    with pytest.raises(Killed):
        await attempt(_stamps(world))
    world.die_before = None
    result = await attempt(_stamps(world))

    assert world.effects == ["a", "b", "c"], "no call ran twice"
    assert result.output == "Done after 4 requests."
    assert [(r["ok"], r["denied_reason"]) for r in journal.entries] == [
        (True, ""),
        (False, UNKNOWN_REASON),
        (True, ""),
    ]
    assert world.provider_calls == 4, "the unknown answer replays exactly: no request diverged"
    assert OUTCOME_UNKNOWN.startswith("OUTCOME UNKNOWN")


async def test_a_read_the_interruption_cut_off_runs_again() -> None:
    """A read changes nothing, so an intent with no result is no unknown outcome: it runs again."""
    step, log, journal = StepId(uuid.uuid4()), MemoryLog(), Journal()
    world = World([[("read_file", {"text": "a"})], [("read_file", {"text": "b"})]])

    async def attempt(toolset: FunctionToolset[Any]) -> Any:
        scope = await CallLogScope.open(log, step, fresh=False)
        return await _runner(world, toolset, journal, step=step).run("read", log=scope)

    with pytest.raises(Killed):
        await attempt(_stamps(world, die_on="b", side="read_file"))
    result = await attempt(_stamps(world, side="read_file"))

    assert world.effects == ["a", "b", "b"], "the cut-off read ran again"
    assert result.output == "Done after 3 requests."
    assert [(r["ok"], r["denied_reason"]) for r in journal.entries] == [(True, ""), (True, "")]
    returns = [p for m in result.all_messages() for p in m.parts if p.part_kind == "tool-return"]
    assert [p.content for p in returns] == ["stamped a", "stamped b"]


async def test_egress_denials_are_journalled_from_the_proxy() -> None:
    """The refusal happened in another process, on another network. Without this the flight
    recorder shows a curl that failed for no stated reason."""
    journal = Journal()
    await journal_egress_denials(
        journal, [{"ip": "172.20.0.3", "host": "pypi.org", "reason": "not in allowlist ['example.com']"}]
    )
    [entry] = journal.entries
    assert entry["tool"] == "frame.egress"
    assert entry["ok"] is False
    assert entry["summary"] == "host=pypi.org"  # labels.egress_label - the format the web parses
    assert "not in allowlist" in str(entry["denied_reason"])


async def test_egress_denials_count_a_host_once_and_skip_the_browsers_own_calls() -> None:
    journal = Journal()
    denied = {"ip": "172.20.0.3", "reason": "not in allowlist"}
    hosts = ["upload.wikimedia.org"] * 3 + ["update.googleapis.com"]
    await journal_egress_denials(journal, [{**denied, "host": h} for h in hosts])
    assert [(e["summary"], e["denied_reason"]) for e in journal.entries] == [
        ("host=upload.wikimedia.org", "not in allowlist (3 times)")
    ]
