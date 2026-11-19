# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Within a step the context stays under its threshold (`capabilities/compaction.py`): a long scripted
run on a real agent loop, the spill read back through its pointer, the provider's own count driving
the threshold, the same history compacting the same way, and a death after a compaction re-entering
on its call log without buying a request twice."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from pydantic_ai import Agent, DeferredToolRequests
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.usage import RequestUsage, UsageLimits

from prismoid.ai.agents.base import build
from prismoid.ai.agents.run import AgentRunner, TapSlot
from prismoid.ai.capabilities.compaction import (
    CLEARED,
    HANDOFF,
    SPILL_AT,
    TRIGGER_SHARE,
    Compaction,
    Summarize,
    compacted,
    estimated,
    handed_off,
    handoff_cut,
    history_tokens,
    spilled,
)
from prismoid.ai.capabilities.durability import CallLogScope, PrismoidDurability, request_fingerprint
from prismoid.ai.deps import TaskDeps
from prismoid.ai.frames.paper import PaperFrame
from prismoid.ai.gateway import resolve, throttle
from prismoid.ai.gateway.classes import ModelSpec
from prismoid.ai.gateway.providers import DEFAULT_MAX_TOKENS
from prismoid.schemas.ids import StepId
from prismoid.schemas.model_class import Family, ModelClass
from prismoid.schemas.tokens import TOKENS
from tests.support.durability_pins import full_catalog
from tests.support.rigs.durability_drill import Killed, MemoryLog
from tests.support.runners import PLAIN

WINDOW = 40_000  # usable tokens in these runs: a clear fires at 20k
OVERHEAD = 1_500  # what a provider counts beyond the messages (instructions, tool definitions)
BIG = "x" * (4 * SPILL_AT + 400)  # one result no tool bounded


def _paper() -> PaperFrame:
    return PaperFrame("t", max_bytes=10_000_000)


def _usage(messages: list[ModelMessage], output: int) -> RequestUsage:
    """What a provider reports: the request's real size, here its messages plus a fixed overhead."""
    return RequestUsage(input_tokens=estimated(messages) + OVERHEAD, output_tokens=output)


def _calls(messages: list[ModelMessage]) -> int:
    """The scripted run's next call number, read from the newest call: a handoff drops old calls."""
    for message in reversed(messages):
        for part in message.parts:
            if isinstance(part, ToolCallPart):
                return int(part.args_as_dict()["n"]) + 1
    return 1


# ── a 200-call run on a real agent loop ─────────────────────────────────────────────────────────


def _records() -> FunctionToolset[Any]:
    """`look`: one record a call, and every 50th a result no tool bounded."""
    toolset: FunctionToolset[Any] = FunctionToolset()

    def look(n: int) -> str:
        """Read one record."""
        return BIG if n % 50 == 0 else f"record {n}\n" + "field: value\n" * 400

    toolset.add_function(look)
    return toolset


async def _long_run(
    bound: Compaction,
    frame: PaperFrame | None = None,
    *,
    seat: Callable[[FunctionModel], Model] = lambda m: m,
) -> tuple[list[int], set[str]]:
    """A 200-call scripted run, each turn a note and a call, served by `seat` around the script: what
    each request carried, as counted, and every spill file a request named."""
    sent: list[int] = []
    named: set[str] = set()
    note = "I read the result and will look at the next record. " * 30  # ~400 tokens a turn

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        sent.append(estimated(messages) + OVERHEAD)
        named.update(_pointers(messages))
        n = _calls(messages)
        if n > 200:
            return ModelResponse(parts=[TextPart("done")], usage=_usage(messages, 5))
        parts = [TextPart(note), ToolCallPart("look", {"n": n})]
        return ModelResponse(parts=parts, usage=_usage(messages, TOKENS.count(note) + 10))

    agent = Agent(seat(FunctionModel(model)), deps_type=TaskDeps, toolsets=[_records()], capabilities=[bound])
    result = await agent.run(
        "Read every record",
        deps=TaskDeps(frame=frame or _paper()),
        usage_limits=UsageLimits(request_limit=None),
    )
    assert result.output == "done" and len(sent) == 201
    return sent, named


def _pointers(messages: list[ModelMessage]) -> list[str]:
    """The files the history's spilled results name."""
    lead = "[The whole result is in "
    return [
        part.content.removeprefix(lead).split(";", 1)[0]
        for m in messages
        for part in m.parts
        if isinstance(part, ToolReturnPart)
        and isinstance(part.content, str)
        and part.content.startswith(lead)
    ]


async def test_a_200_call_run_stays_under_its_threshold() -> None:
    frame = _paper()
    row = _row("openai", "openai", WINDOW + DEFAULT_MAX_TOKENS)
    bound = Compaction.for_seat(ModelClass.FAST_EXECUTOR, [row], None)
    assert bound.window == WINDOW
    sent, named = await _long_run(bound, frame)
    assert max(sent) <= WINDOW, f"the run reached {max(sent)} tokens against a {WINDOW} window"
    assert len(named) == 4, "each of the four results no tool bounded was spilled"
    assert [await frame.read_file(path) for path in sorted(named)] == [BIG] * 4, "spilled whole, not dropped"


async def test_without_a_known_window_the_same_run_overflows() -> None:
    """The bite: a seat whose window the catalog does not know gets the spill alone, and the same
    run goes far past the window."""
    sent, _ = await _long_run(Compaction(window=0))
    assert max(sent) > 5 * WINDOW


# ── the spill ───────────────────────────────────────────────────────────────────────────────────


def _returned(content: str, call_id: str = "call1", tool: str = "connector_read") -> list[ModelMessage]:
    return [
        ModelRequest(parts=[UserPromptPart("go")]),
        ModelResponse(parts=[ToolCallPart(tool, {}, tool_call_id=call_id)]),
        ModelRequest(parts=[ToolReturnPart(tool, content, tool_call_id=call_id)]),
    ]


def _result(messages: list[ModelMessage]) -> str:
    part = messages[-1].parts[0]
    assert isinstance(part, ToolReturnPart) and isinstance(part.content, str)
    return part.content


async def test_a_spilled_result_is_whole_in_the_file_its_pointer_names() -> None:
    frame = _paper()
    whole = "".join(f"row {i}: {'v' * 60}\n" for i in range(2_000))
    said = _result(await spilled(_returned(whole), frame))

    assert TOKENS.count(said) < SPILL_AT // 4, "the model reads the head and tail only"
    first, rest = said.split("\n", 1)
    pointer = first.removeprefix("[The whole result is in ").split(";", 1)[0]
    assert await frame.read_file(pointer) == whole
    assert rest.startswith("row 0:") and said.rstrip().endswith(f"row 1999: {'v' * 60}")


async def test_a_result_under_the_spill_size_is_untouched() -> None:
    history = _returned("short\n" * 100)
    assert await spilled(history, _paper()) == history


async def test_with_no_frame_a_big_result_is_cut_and_names_no_file() -> None:
    said = _result(await spilled(_returned(BIG), None))
    assert TOKENS.count(said) < SPILL_AT // 4 and "The whole result is in" not in said


async def test_a_clear_keeps_the_spilled_file_s_name() -> None:
    frame = _paper()
    history = await spilled(_returned(BIG, "c0"), frame)
    for n in range(1, 10):
        history += _returned(f"result {n}\n" + "y" * 4_000, f"c{n}")[1:]
    assert estimated(history) > 7_000
    out = compacted(history, trigger=7_000)
    first = _result(out[:3])
    assert first.startswith(CLEARED) and "/work/.tool-output/connector_read-c0.txt" in first
    assert await frame.read_file("/work/.tool-output/connector_read-c0.txt") == BIG


# ── the measure: the provider's own count ──────────────────────────────────────────────────────


def _loop(results: int, *, reported: int | None) -> list[ModelMessage]:
    """A tool loop of `results` calls; the last response reports `reported` input tokens."""
    history: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart("go")])]
    for n in range(results):
        usage = (
            RequestUsage(input_tokens=reported or 0, output_tokens=10) if n == results - 1 else RequestUsage()
        )
        history.append(
            ModelResponse(parts=[ToolCallPart("look", {"n": n}, tool_call_id=f"c{n}")], usage=usage)
        )
        history.append(
            ModelRequest(parts=[ToolReturnPart("look", f"record {n}\n" + "z" * 400, tool_call_id=f"c{n}")])
        )
    return history


def test_the_provider_s_count_drives_the_threshold() -> None:
    """Twelve small results estimate far under the trigger, but the provider counted the request at
    30k (instructions, tools, images the estimate cannot see): the clear fires."""
    trigger = 20_000
    small = _loop(12, reported=30_000)
    assert estimated(small) < trigger < history_tokens(small)
    assert any(CLEARED in _text(m) for m in compacted(small, trigger))
    unreported = _loop(12, reported=None)
    assert compacted(unreported, trigger) == unreported, "the bite: by the estimate alone nothing clears"


def test_the_measure_adds_only_what_came_after_the_provider_s_count() -> None:
    history = _loop(3, reported=5_000)
    assert history_tokens(history) == 5_000 + 10 + estimated(history[-1:])
    assert history_tokens(history[:1]) == estimated(history[:1]), "before any response, all estimated"


def _text(message: ModelMessage) -> str:
    return "".join(str(getattr(part, "content", "")) for part in message.parts)


def _code_handoff(history: list[ModelMessage], trigger: int) -> list[ModelMessage]:
    """The whole bound with no summarizer: the clear, then the handoff code writes when one is due."""
    cleared = compacted(history, trigger)
    cut = handoff_cut(history, cleared, trigger)
    return cleared if cut is None else handed_off(cleared, cut, None)


# ── clear, then hand off ────────────────────────────────────────────────────────────────────────


def test_a_clear_keeps_the_newest_results_and_takes_the_rest_in_one_batch() -> None:
    history = _loop(20, reported=None)
    out = compacted(history, trigger=estimated(history) - 100)
    results = [p for m in out for p in m.parts if isinstance(p, ToolReturnPart)]
    assert [str(p.content).startswith(CLEARED) for p in results] == [True] * 14 + [False] * 6
    assert len(out) == len(history), "a clear drops nothing"


def test_a_cleared_result_is_never_cleared_again() -> None:
    history = _loop(20, reported=None)
    once = compacted(history, trigger=100)
    assert request_fingerprint(compacted(once, trigger=100)) == request_fingerprint(once)


def test_when_a_clear_is_not_enough_the_oldest_exchanges_are_handed_off() -> None:
    history: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart("Write the report")])]
    for n in range(30):
        history.append(
            ModelResponse(
                parts=[
                    TextPart(f"note {n} " + "w" * 4_000),
                    ToolCallPart("look", {"n": n}, tool_call_id=f"c{n}"),
                ]
            )
        )
        history.append(ModelRequest(parts=[ToolReturnPart("look", f"record {n}", tool_call_id=f"c{n}")]))
    out = _code_handoff(history, trigger=8_000)

    assert estimated(out) <= 8_000
    head = out[0]
    assert isinstance(head, ModelRequest) and _text(head).startswith("Write the report")
    last = head.parts[-1]
    assert isinstance(last, SystemPromptPart)
    handoff = str(last.content)
    assert handoff.startswith(HANDOFF) and "- called look: record" in handoff
    assert isinstance(out[1], ModelResponse), "the run goes on from a whole exchange"
    calls = {p.tool_call_id for m in out for p in m.parts if isinstance(p, ToolCallPart)}
    returns = {p.tool_call_id for m in out for p in m.parts if isinstance(p, ToolReturnPart)}
    assert calls == returns, "every kept call keeps its result"


def test_a_second_handoff_folds_the_first_and_stays_short() -> None:
    history: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart("go")])]
    for n in range(200):
        history.append(
            ModelResponse(parts=[TextPart("w" * 2_000), ToolCallPart("look", {"n": n}, tool_call_id=f"c{n}")])
        )
        history.append(ModelRequest(parts=[ToolReturnPart("look", f"record {n}", tool_call_id=f"c{n}")]))
    once = _code_handoff(history[:201], trigger=6_000)
    twice = _code_handoff([*once, *history[201:]], trigger=6_000)
    handoffs = [
        p for p in twice[0].parts if isinstance(p, SystemPromptPart) and str(p.content).startswith(HANDOFF)
    ]
    assert len(handoffs) == 1
    assert "earlier lines not kept" in str(handoffs[0].content)
    assert TOKENS.count(str(handoffs[0].content)) < 2_500


# ── determinism ─────────────────────────────────────────────────────────────────────────────────


async def test_the_same_history_compacts_to_the_same_request() -> None:
    def history() -> list[ModelMessage]:
        out: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart("go")])]
        for n in range(40):
            content = BIG if n in (3, 39) else f"record {n}\n" + "q" * 3_000
            out.append(
                ModelResponse(
                    parts=[
                        TextPart(f"n{n} " + "t" * 1_500),
                        ToolCallPart("look", {"n": n}, tool_call_id=f"c{n}"),
                    ]
                )
            )
            out.append(ModelRequest(parts=[ToolReturnPart("look", content, tool_call_id=f"c{n}")]))
        return out

    one = _code_handoff(await spilled(history(), _paper()), trigger=12_000)
    two = _code_handoff(await spilled(history(), _paper()), trigger=12_000)
    assert request_fingerprint(one) == request_fingerprint(two), "timestamps aside, the same request"
    assert request_fingerprint(one) != request_fingerprint(history()), "the history was compacted"


# ── the seat ────────────────────────────────────────────────────────────────────────────────────


def _row(
    provider: str, protocol: str, window: int, classes: tuple[ModelClass, ...] = (ModelClass.FAST_EXECUTOR,)
) -> ModelSpec:
    return ModelSpec(provider, f"{provider}-m", Family(provider), protocol, classes, window)


@pytest.mark.parametrize(
    ("model_class", "served", "catalog", "cleared", "window"),
    [
        (ModelClass.FAST_EXECUTOR, "openai", ["openai"], [], 400_000),
        (ModelClass.FAST_EXECUTOR, None, ["openai", "anthropic"], [], 200_000),
        (ModelClass.COMPUTER_USE, "anthropic", ["anthropic"], ["anthropic"], 200_000),
        (ModelClass.COMPUTER_USE, None, ["openai", "google"], [], 400_000),
        (ModelClass.COMPUTER_USE, None, ["openai", "anthropic"], ["anthropic"], 200_000),
    ],
)
def test_the_bound_follows_the_seats_that_may_answer(
    model_class: ModelClass, served: str | None, catalog: list[str], cleared: list[str], window: int
) -> None:
    windows = {"openai": 400_000, "google": 1_000_000, "anthropic": 200_000}
    rows = [_row(p, p, windows[p], (model_class,)) for p in catalog]
    seat = next((r for r in rows if r.provider == served), None)
    bound = Compaction.for_seat(model_class, rows, seat)
    assert bound.cleared_rows == {f"{p}:{p}-m" for p in cleared}
    assert bound.window == window - DEFAULT_MAX_TOKENS


def _chain(
    monkeypatch: pytest.MonkeyPatch, model_class: ModelClass, rows: list[tuple[str, bool]]
) -> tuple[list[ModelSpec], Callable[[FunctionModel], Model]]:
    """The real `resolve()` chain over `(provider, healthy)` rows: a healthy row serves the script,
    the others answer every request with a 529."""
    specs = [_row(p, p, WINDOW + DEFAULT_MAX_TOKENS, (model_class,)) for p, _ in rows]

    def browned_out(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise ModelHTTPError(529, "overloaded")

    def seat(script: FunctionModel) -> Model:
        built = {f"{p}-m": script if healthy else FunctionModel(browned_out) for p, healthy in rows}
        monkeypatch.setattr(resolve, "build_model", lambda **row: built[row["model_id"]])
        throttle.reset()
        return resolve.resolve(model_class, specs, order={model_class.value: [s.ref for s in specs]})

    return specs, seat


async def test_an_anthropic_seat_outside_computer_use_is_bounded_on_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bite: a maker on Anthropic once had no bound at all (its server clears only computer use)."""
    specs, seat = _chain(monkeypatch, ModelClass.FAST_EXECUTOR, [("anthropic", True)])
    sent, _ = await _long_run(Compaction.for_seat(ModelClass.FAST_EXECUTOR, specs, None), seat=seat)
    assert max(sent) <= WINDOW


async def test_a_failover_from_a_clearing_seat_compacts_on_the_row_that_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An Anthropic head that clears server-side never switches off the client bound of the row that
    really answers: the decision is per row, never for the whole chain."""
    rows = [("anthropic", False), ("openai", True)]
    specs, seat = _chain(monkeypatch, ModelClass.COMPUTER_USE, rows)
    sent, _ = await _long_run(Compaction.for_seat(ModelClass.COMPUTER_USE, specs, None), seat=seat)
    assert max(sent) <= WINDOW


async def test_a_seat_whose_server_clears_is_never_edited_on_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Anthropic clears a computer-use run itself, so the client sends the history it holds, unedited."""
    specs, seat = _chain(monkeypatch, ModelClass.COMPUTER_USE, [("anthropic", True), ("openai", True)])
    sent, _ = await _long_run(Compaction.for_seat(ModelClass.COMPUTER_USE, specs, None), seat=seat)
    assert max(sent) > 5 * WINDOW


def test_a_window_the_catalog_does_not_know_is_no_window() -> None:
    assert Compaction.for_seat(ModelClass.FAST_EXECUTOR, [_row("openai", "openai", 0)], None).window == 0


# ── a death after a compaction (the call log, in process) ───────────────────────────────────────

STEP = StepId(uuid.uuid4())
LAST = 16  # the scripted step's calls; a clear fires from about call 8


class Provider:
    """A scripted provider that counts what it serves and dies as request `die_before` is sent, or as
    the first request carrying a handoff is (`stop_at_handoff`, keeping what it was sent). `notes`
    adds a long note to each turn, so a clear is not enough and the run hands off."""

    def __init__(
        self, die_before: int | None = None, *, notes: bool = False, stop_at_handoff: bool = False
    ) -> None:
        self.served = 0
        self.die_before = die_before
        self.notes = notes
        self.stop_at_handoff = stop_at_handoff
        self.saw_cleared = False
        self.handoff: list[ModelMessage] = []

    def respond(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        n = _calls(messages)
        handed_off_now = any(HANDOFF in _text(m) for m in messages)
        if n == self.die_before or (self.stop_at_handoff and handed_off_now):
            self.handoff = list(messages)
            raise Killed
        self.served += 1
        self.saw_cleared |= handed_off_now or any(CLEARED in _text(m) for m in messages)
        if n > LAST:
            return ModelResponse(parts=[TextPart("done")], usage=_usage(messages, 5))
        note = [TextPart("note " + "n" * 12_000)] if self.notes else []
        return ModelResponse(parts=[*note, ToolCallPart("look", {"n": n})], usage=_usage(messages, 10))


def _drill_runner(provider: Provider, ran: list[int], summarize: Summarize | None = None) -> AgentRunner:
    toolset: FunctionToolset[Any] = FunctionToolset(id="drill")

    def look(n: int) -> str:
        """Read one record."""
        ran.append(n)
        return f"record {n}\n" + "r" * 6_000

    toolset.add_function(look)
    bound = Compaction(window=int(12_000 / TRIGGER_SHARE), summarize=summarize)
    agent = Agent(
        FunctionModel(provider.respond),
        name="drill",
        deps_type=TaskDeps,
        toolsets=[toolset],
        output_type=[str, DeferredToolRequests],
        capabilities=[bound, PrismoidDurability()],
    )
    return AgentRunner(agent, TaskDeps(step_id=STEP), PLAIN, live=TapSlot())


async def _logged(runner: AgentRunner, log: MemoryLog) -> Any:
    return await runner.run("Read every record", log=await CallLogScope.open(log, STEP, fresh=False))


async def test_a_death_after_a_compaction_resumes_on_the_compacted_history_and_rebuys_nothing() -> None:
    clean, clean_ran = Provider(), list[int]()
    base = await _logged(_drill_runner(clean, clean_ran), MemoryLog())
    assert clean.saw_cleared, "the drill dies after a compaction"

    log, dying, ran = MemoryLog(), Provider(die_before=LAST - 2), list[int]()
    with pytest.raises(Killed):
        await _logged(_drill_runner(dying, ran), log)
    assert dying.saw_cleared
    resumed = Provider()
    result = await _logged(_drill_runner(resumed, ran), log)

    assert result.output == base.output == "done"
    assert dying.served + resumed.served == clean.served, "no request is bought twice"
    assert ran == clean_ran, "no tool call runs twice"
    assert len(log.rows) == 1, "the replay matched every fingerprint: no fence, one generation"


# ── the summarizer writes the handoff, logged ───────────────────────────────────────────────────


class Summarizer:
    """A scripted summarizer: counts its calls, answers a different line each time, or fails."""

    def __init__(self, *, fails: bool = False) -> None:
        self.calls = 0
        self.fails = fails

    async def __call__(self, transcript: str) -> str:
        self.calls += 1
        if self.fails:
            raise RuntimeError("the decider is down")
        assert "Read every record" in transcript, "the summarizer reads what was asked"
        return f"Asked: read every record. Findings: records 1 on, read. (summary {self.calls})"


async def _first_handoff(summarize: Summarizer) -> list[ModelMessage]:
    """A logged run up to the first request that carries a handoff: what that request sent."""
    provider = Provider(notes=True, stop_at_handoff=True)
    with pytest.raises(Killed):
        await _logged(_drill_runner(provider, [], summarize), MemoryLog())
    assert provider.handoff, "the run handed off"
    return provider.handoff


async def test_a_handoff_is_written_by_the_summarizer_once() -> None:
    summarize = Summarizer()
    head = _text((await _first_handoff(summarize))[0])
    assert summarize.calls == 1
    assert f"{HANDOFF}\nAsked: read every record." in head and "- called look" not in head


async def test_a_failed_summary_falls_back_to_the_handoff_code_writes() -> None:
    summarize = Summarizer(fails=True)
    head = _text((await _first_handoff(summarize))[0])
    assert summarize.calls == 1 and "- called look: record" in head


async def test_a_replay_reads_the_recorded_summary_and_calls_no_model() -> None:
    clean, summarize = Provider(notes=True), Summarizer()
    base = await _logged(_drill_runner(clean, [], summarize), MemoryLog())
    assert summarize.calls >= 1, "the clean run handed off"

    log, dying, again = MemoryLog(), Provider(LAST - 1, notes=True), Summarizer()
    with pytest.raises(Killed):
        await _logged(_drill_runner(dying, [], again), log)
    assert again.calls >= 1, "the run died after a handoff"
    resumed = Provider(notes=True)
    result = await _logged(_drill_runner(resumed, [], again), log)

    assert result.output == base.output
    assert again.calls == summarize.calls, "a recorded summary is read back, never bought again"
    assert dying.served + resumed.served == clean.served and len(log.rows) == 1


def test_every_build_bounds_its_history_with_the_summarizer_beside_it() -> None:
    found: list[object] = []
    build("writer", TaskDeps(), full_catalog()).agent.root_capability.apply(found.append)
    bounds = [c for c in found if isinstance(c, Compaction)]
    assert len(bounds) == 1 and bounds[0].summarize is not None
