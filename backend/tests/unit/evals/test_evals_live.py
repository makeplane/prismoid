# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The live runner against a scripted API: no stack, no model, no spend.

A fake API serves the record a turn would have left, frame by frame, on a fake clock that each frame
sets to its own time as it is read. So "the first delta arrived 1.2 s after submit" is a fact of the
script, not of this machine, and every number a trace carries is checked against the script that
produced it.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
import pytest

from evals.runners import InfraError, ThreadStalled, TrialTimeout, live
from evals.runners.live import LiveRunner, connected_slugs, setup_gap
from evals.schema import (
    CheckKind,
    MemoryWritten,
    Outcome,
    OutcomeRow,
    PriorTurn,
    RowContext,
    Surface,
    Unscored,
)
from prismoid.schemas.events import (
    GateOpened,
    MemoryStub,
    MessageDelta,
    MessageStart,
    TaskContinued,
    TaskCreated,
    TaskDelivered,
    TaskDequeued,
    TaskPlanned,
    UsageRecorded,
)
from prismoid.schemas.events import (
    MemoryWritten as MemoryWrittenEvent,
)
from prismoid.schemas.ids import GateId, MemoryId, StepId, TaskId
from prismoid.schemas.model_class import Family
from prismoid.schemas.plan import GateKind, PlanStepView, PlanView, StepKind, StepState, TaskMode
from prismoid.schemas.usage import Unit

START = dt.datetime(2026, 11, 14, 9, 0, tzinfo=dt.UTC)
TASK = TaskId(uuid4())


class Clock:
    """The runner's `now`, moved by the stream: a frame is read at the time it carries."""

    def __init__(self) -> None:
        self.at = START

    def __call__(self) -> dt.datetime:
        return self.at


class Record:
    """A task's frames, built by hand, one list for the whole thread. `starts` marks where each turn begins."""

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self.starts: list[int] = []
        self._seq = 0

    def _durable(self, make: Any, at_s: float, **fields: Any) -> Record:
        self._seq += 1
        event = make(seq=self._seq, task_id=TASK, at=START + dt.timedelta(seconds=at_s), **fields)
        self.frames.append(event.model_dump(mode="json"))
        return self

    def created(self, at_s: float = 0.0) -> Record:
        self.starts.append(len(self.frames))
        return self._durable(TaskCreated, at_s, goal="g", mode=TaskMode.STANDARD)

    def continued(self, turn: int, at_s: float) -> Record:
        self.starts.append(len(self.frames))
        return self._durable(TaskContinued, at_s, message="more", turn=turn)

    def dequeued(self, at_s: float) -> Record:
        return self._durable(TaskDequeued, at_s)

    def planned(self, at_s: float) -> Record:
        step = PlanStepView(
            id=StepId(uuid4()), title="s", kind=StepKind.STEP, state=StepState.PENDING, lane_role="express"
        )
        return self._durable(TaskPlanned, at_s, plan=PlanView(steps=[step]), route="express")

    def start(self, at_s: float, channel: str = "c") -> Record:
        event = MessageStart(
            task_id=TASK, at=START + dt.timedelta(seconds=at_s), after_seq=self._seq, channel=channel
        )
        self.frames.append(event.model_dump(mode="json"))
        return self

    def delta(self, at_s: float, channel: str = "c") -> Record:
        event = MessageDelta(
            task_id=TASK,
            at=START + dt.timedelta(seconds=at_s),
            after_seq=self._seq,
            channel=channel,
            text="hi",
        )
        self.frames.append(event.model_dump(mode="json"))
        return self

    def failed(self, at_s: float, reason: str) -> Record:
        self._seq += 1
        at = (START + dt.timedelta(seconds=at_s)).isoformat()
        self.frames.append(
            {
                "type": "task.status",
                "status": "failed",
                "lane": "record",
                "seq": self._seq,
                "task_id": str(TASK),
            }
            | {"at": at, "reason": reason, "step_id": None, "project_id": None, "actor": None}
        )
        return self

    def usage(self, at_s: float, cost: str) -> Record:
        return self._durable(
            UsageRecorded,
            at_s,
            provider="p",
            model="m",
            family=Family("anthropic"),
            units={Unit.REQUEST: 1},
            cost_usd=Decimal(cost),
        )

    def gate(self, at_s: float, kind: GateKind) -> Record:
        return self._durable(GateOpened, at_s, gate_id=GateId(uuid4()), kind=kind, title="?")

    def delivered(self, at_s: float, summary: str = "done") -> Record:
        return self._durable(TaskDelivered, at_s, summary=summary)

    def remembered(self, at_s: float, text: str) -> Record:
        stub = MemoryStub(id=MemoryId(uuid4()), kind="fact", text=text)
        return self._durable(MemoryWrittenEvent, at_s, count=1, items=[stub])

    def seq_of_start(self, turn: int) -> int:
        return int(self.frames[self.starts[turn - 1]]["seq"])


class Api:
    """The API a runner talks to: it serves `record` up to the turns accepted so far."""

    def __init__(self, record: Record, clock: Clock) -> None:
        self.record, self.clock = record, clock
        self.accepted = 0
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.cancelled = 0
        self.hang_after_frames = False  # the stream goes quiet once the record is served
        self.refuse_follow_up: int | None = None  # the status a follow-up POST answers, if not accepted
        self.quiet = asyncio.Event()  # set when a hanging stream has served all it will

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/cancel"):
            self.cancelled += 1
            return httpx.Response(200, json={})
        if request.method == "POST" and path.endswith("/follow_up") and self.refuse_follow_up:
            return httpx.Response(self.refuse_follow_up, json={"error": {"code": "refused"}})
        if request.method == "POST":
            self.posts.append((path, json.loads(request.content)))
            self.accepted += 1
            return httpx.Response(
                202, json={"task_id": str(TASK), "turn": self.accepted, "last_seq": self._head()}
            )
        return httpx.Response(200, stream=_Sse(self))

    def _head(self) -> int:
        return self.record.seq_of_start(self.accepted)


class _Sse(httpx.AsyncByteStream):
    def __init__(self, api: Api) -> None:
        self._api = api

    async def __aiter__(self) -> AsyncIterator[bytes]:
        api = self._api
        end = api.record.starts[api.accepted] if api.accepted < len(api.record.starts) else None
        for frame in api.record.frames[:end]:
            api.clock.at = max(api.clock.at, dt.datetime.fromisoformat(frame["at"]))
            yield f"event: {frame['type']}\ndata: {json.dumps(frame)}\n\n".encode()
            await asyncio.sleep(0)
        if api.hang_after_frames:
            api.quiet.set()
            await asyncio.sleep(3600)


def row(message: str = "hello", **fields: Any) -> OutcomeRow:
    return OutcomeRow(id="r", message=message, expected_outcome=Outcome.REPLY, **fields)


async def run(api: Api, clock: Clock, subject: OutcomeRow, *, timeout_s: float = 5.0):
    async with httpx.AsyncClient(transport=api.transport(), base_url="http://api") as client:
        runner = LiveRunner(client, budget_usd=0.5, timeout_s=timeout_s, now=clock)
        return await runner(subject)


def a_reply() -> Record:
    return (
        Record()
        .created()
        .dequeued(0.4)
        .planned(0.5)
        .start(1.1)
        .delta(1.2)
        .delta(1.5)
        .usage(2.0, "0.002")
        .delivered(2.4, "a reply")
    )


# ── what a turn measures ─────────────────────────────────────────────────────────────────────


async def test_a_reply_is_measured_from_the_clients_submit_and_the_durable_record() -> None:
    clock = Clock()
    api = Api(a_reply(), clock)
    trace = await run(api, clock, row())
    assert trace.outcome is Outcome.REPLY
    assert trace.final_text == "a reply"
    assert trace.ttft_s == pytest.approx(1.2)  # the first delta, not the second
    assert trace.latency_s == pytest.approx(2.4)
    assert trace.cost_usd == Decimal("0.002")
    assert api.cancelled == 0


async def test_the_ask_carries_the_per_task_budget_and_the_rows_setup() -> None:
    clock = Clock()
    api = Api(a_reply(), clock)
    subject = row(context=RowContext(dials={"deliverable": "deck", "mode": "fleet"}, connectors=("slack",)))
    await run(api, clock, subject)
    assert api.posts == [
        (
            "/v1/tasks",
            {
                "goal": "hello",
                "budget": {"usd": 0.5},
                "deliverable": "deck",
                "mode": "fleet",
                "connectors": ["slack"],
            },
        )
    ]


async def test_a_turn_that_never_streams_a_delta_has_no_ttft() -> None:
    clock = Clock()
    record = Record().created().planned(0.5).usage(1.0, "0.001").delivered(1.5)
    trace = await run(Api(record, clock), clock, row())
    assert trace.ttft_s is None
    assert trace.latency_s == pytest.approx(1.5)


async def test_a_delta_whose_message_began_before_the_stream_opened_gives_no_ttft() -> None:
    clock = Clock()
    record = Record().created().planned(0.5).delta(1.2).start(1.4, "c2").delta(1.5, "c2").delivered(2.0)
    trace = await run(Api(record, clock), clock, row())
    assert trace.ttft_s is None  # never the later message's 1.5 s
    assert trace.latency_s == pytest.approx(2.0)


async def test_what_lands_after_delivery_is_neither_waited_for_nor_costed() -> None:
    clock = Clock()
    record = a_reply().usage(9.0, "0.5").delivered(9.5)  # a verdict's spend, and a second delivery
    trace = await run(Api(record, clock), clock, row())
    assert trace.cost_usd == Decimal("0.002")
    assert trace.calls_before_delivery == 1


# ── when a turn ends without delivering ──────────────────────────────────────────────────────


async def test_a_gate_waiting_on_a_person_ends_the_turn_as_an_ask_and_stops_the_task() -> None:
    clock = Clock()
    api = Api(Record().created().dequeued(0.4).gate(0.9, GateKind.INPUT), clock)
    api.hang_after_frames = True  # nobody answers; the stream goes quiet
    trace = await run(api, clock, row())
    assert trace.outcome is Outcome.ASK
    assert api.cancelled == 1


async def test_a_turn_that_outlives_its_clock_is_a_timeout_carrying_what_it_did() -> None:
    clock = Clock()
    api = Api(Record().created().dequeued(0.4).planned(0.5), clock)
    api.hang_after_frames = True
    with pytest.raises(TrialTimeout) as late:
        await run(api, clock, row(), timeout_s=0.05)
    assert late.value.trace.outcome is None
    assert not late.value.trace.delivered
    assert api.cancelled == 1


async def test_a_task_that_fails_is_a_turn_with_no_ending_not_an_infra_error() -> None:
    clock = Clock()
    failed = Record().created().dequeued(0.4).failed(1.0, "InvalidPlan: the plan has a cycle")
    trace = await run(Api(failed, clock), clock, row())
    assert trace.outcome is None
    assert not trace.delivered


@pytest.mark.parametrize(
    "reason",
    [
        "ModelRateLimitError: status_code: 429, model_name: m, body: slow down",
        "ModelOverloadedError: status_code: 529, model_name: m",
        "FallbackExceptionGroup: All models from FallbackModel failed (3 sub-exceptions)",
    ],
)
async def test_a_turn_the_model_provider_failed_is_infra_not_the_agents_miss(reason: str) -> None:
    clock = Clock()
    failed = Record().created().dequeued(0.4).planned(0.5).failed(1.0, reason)
    with pytest.raises(InfraError, match="provider"):
        await run(Api(failed, clock), clock, row())


async def test_the_api_refusing_the_ask_is_a_reject_not_an_error() -> None:
    def refuse(_: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"error": {"code": "invalid"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(refuse), base_url="http://api") as client:
        trace = await LiveRunner(client, budget_usd=1, timeout_s=1)(row())
    assert trace.outcome == "reject"


# ── infra is not the agent's failure ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("status", [500, 502, 429])
async def test_a_server_error_or_a_throttle_on_submit_is_infra(status: int) -> None:
    def down(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status)

    async with httpx.AsyncClient(transport=httpx.MockTransport(down), base_url="http://api") as client:
        with pytest.raises(InfraError):
            await LiveRunner(client, budget_usd=1, timeout_s=1)(row())


async def test_a_dropped_connection_is_infra() -> None:
    def drop(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(drop), base_url="http://api") as client:
        with pytest.raises(InfraError):
            await LiveRunner(client, budget_usd=1, timeout_s=1)(row())


async def test_a_stream_that_closes_before_the_turn_ended_is_infra_and_the_task_is_cancelled() -> None:
    clock = Clock()
    api = Api(Record().created().dequeued(0.4), clock)
    with pytest.raises(InfraError):
        await run(api, clock, row())
    assert api.cancelled == 1  # the retry's new task must not run beside a live orphan


async def test_a_bad_token_stops_the_run_instead_of_being_retried_as_infra() -> None:
    def unauthorized(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"code": "unauthorized"}})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(unauthorized), base_url="http://api"
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await LiveRunner(client, budget_usd=1, timeout_s=1)(row())


# ── a thread ─────────────────────────────────────────────────────────────────────────────────


def a_thread() -> Record:
    return (
        Record()
        .created()
        .planned(0.5)
        .usage(1.0, "0.010")
        .delivered(2.0, "first answer")
        .continued(2, at_s=2.0)  # sent the moment the first turn ended
        .planned(2.5)
        .start(3.0)
        .delta(3.0)
        .usage(3.5, "0.003")
        .delivered(4.0, "shorter")
    )


async def test_a_thread_row_plays_its_earlier_turns_and_scores_only_its_own() -> None:
    clock = Clock()
    api = Api(a_thread(), clock)
    subject = row("make it shorter", context=RowContext(turns=("write it",)))
    trace = await run(api, clock, subject)
    assert [path for path, _ in api.posts] == ["/v1/tasks", f"/v1/tasks/{TASK}/follow_up"]
    assert [body["goal"] for _, body in api.posts] == ["write it", "make it shorter"]
    assert trace.final_text == "shorter"
    assert trace.prior_text == "first answer"
    assert trace.cost_usd == Decimal("0.003")  # the first turn's 0.010 is not this turn's
    assert trace.ttft_s == pytest.approx(1.0)  # from this turn's own submit
    assert trace.latency_s == pytest.approx(2.0)


async def test_the_first_turns_delivery_does_not_end_the_second_turn_early() -> None:
    clock = Clock()
    api = Api(a_thread(), clock)
    trace = await run(api, clock, row("shorter", context=RowContext(turns=("write it",))))
    assert trace.delivered and trace.final_text == "shorter"


async def test_the_user_lines_of_prior_turns_are_played_and_the_assistants_are_not() -> None:
    clock = Clock()
    api = Api(a_thread(), clock)
    subject = row(
        "shorter",
        context=RowContext(turn=2),
        prior_turns=(PriorTurn(role="user", text="write it"), PriorTurn(role="assistant", text="scripted")),
    )
    await run(api, clock, subject)
    assert [body["goal"] for _, body in api.posts] == ["write it", "shorter"]


async def test_an_earlier_turn_that_asks_is_the_agents_doing_not_infra() -> None:
    clock = Clock()
    api = Api(Record().created().dequeued(0.4).gate(0.9, GateKind.INPUT), clock)
    with pytest.raises(ThreadStalled, match="turn 1 of the thread did not deliver"):
        await run(api, clock, row("shorter", context=RowContext(turns=("write it",))))
    assert api.cancelled == 1  # the task parked on its gate stops spending


async def test_a_refused_follow_up_stops_the_run_and_the_task_it_created() -> None:
    clock = Clock()
    api = Api(a_thread(), clock)
    api.refuse_follow_up = 401
    with pytest.raises(httpx.HTTPStatusError):
        await run(api, clock, row("shorter", context=RowContext(turns=("write it",))))
    assert api.cancelled == 1


async def test_a_cancelled_run_stops_the_task_it_created() -> None:
    clock = Clock()
    api = Api(Record().created().dequeued(0.4).planned(0.5), clock)
    api.hang_after_frames = True
    trial = asyncio.create_task(run(api, clock, row(), timeout_s=3600))
    await asyncio.wait_for(api.quiet.wait(), 5)
    trial.cancel()
    with pytest.raises(asyncio.CancelledError):
        await trial
    assert api.cancelled == 1


async def test_a_turn_of_which_nothing_arrives_in_time_is_infra() -> None:
    async def silence() -> AsyncIterator[bytes]:
        await asyncio.sleep(3600)
        yield b""

    class Silent(httpx.AsyncByteStream):
        def __aiter__(self) -> AsyncIterator[bytes]:
            return silence()

    def answer(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"task_id": str(TASK), "last_seq": 1})
        return httpx.Response(200, stream=Silent())

    async with httpx.AsyncClient(transport=httpx.MockTransport(answer), base_url="http://api") as client:
        with pytest.raises(InfraError, match="nothing of turn"):
            await LiveRunner(client, budget_usd=1, timeout_s=0.05)(row())


# ── what lands after delivery, when a row reads it ───────────────────────────────────────────


def a_remembering_row() -> OutcomeRow:
    return row(checks=(MemoryWritten(type=CheckKind.MEMORY_WRITTEN, contains="April"),))


async def test_a_row_that_checks_a_memory_write_waits_for_it() -> None:
    clock = Clock()
    record = a_reply().remembered(5.0, "birthday in April")
    trace = await run(Api(record, clock), clock, a_remembering_row())
    assert trace.memory_written == ("birthday in April",)


async def test_the_wait_for_a_late_event_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(live, "AFTERMATH_S", 0.05)
    clock = Clock()
    api = Api(a_reply(), clock)
    api.hang_after_frames = True  # the memory write never comes
    trace = await asyncio.wait_for(run(api, clock, a_remembering_row(), timeout_s=3600), 5)
    assert trace.delivered
    assert trace.memory_written == ()


# ── what a row needs that this runner cannot set up ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("context", "why"),
    [
        (RowContext(surface=Surface.CONVERSATION), "chat lane"),
        (RowContext(facts={"turn": 9}), "facts"),
        (RowContext(prior_host="shop.example"), "site"),
        (RowContext(transact=True), "transact"),
        (RowContext(deployment="no search"), "deployment"),
        (RowContext(inputs=("tests/fixtures/a.md",)), "attachments"),
        (RowContext(turn=2), "turn 2"),
        (RowContext(dials={"scale": "long"}), "scale"),
        (RowContext(connectors=("slack",)), "slack"),
    ],
)
def test_a_row_the_runner_cannot_set_up_says_why(context: RowContext, why: str) -> None:
    assert why in (setup_gap(row(context=context), frozenset()) or "")


def test_a_row_it_can_set_up_has_no_gap() -> None:
    assert setup_gap(row(context=RowContext(dials={"deliverable": "deck"})), frozenset()) is None
    assert setup_gap(row(context=RowContext(turns=("first",))), frozenset()) is None
    assert setup_gap(row(context=RowContext(connectors=("slack",))), frozenset({"slack"})) is None


def test_a_dry_run_does_not_know_the_connectors_and_asks_nothing_of_them() -> None:
    assert setup_gap(row(context=RowContext(connectors=("slack",))), None) is None


def test_dials_on_a_later_turn_are_a_gap_because_they_ride_the_first() -> None:
    thread = row(context=RowContext(turns=("first",), dials={"deliverable": "deck"}))
    assert "first turn" in (setup_gap(thread, frozenset()) or "")


def test_an_unscored_row_is_never_run() -> None:
    unscored = OutcomeRow(id="u", message="m", expected_outcome=Unscored.MECHANISM)
    assert "never scored" in (setup_gap(unscored, frozenset()) or "")


async def test_the_connected_slugs_are_the_live_ones_across_pages() -> None:
    pages = {
        None: {
            "items": [{"slug": "slack", "status": "live"}, {"slug": "gmail", "status": "needs_reauth"}],
            "next_cursor": "p2",
        },
        "p2": {"items": [{"slug": "notion", "status": "live"}], "next_cursor": None},
    }

    def serve(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pages[request.url.params.get("cursor")])

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve), base_url="http://api") as client:
        assert await connected_slugs(client) == {"slack", "notion"}
