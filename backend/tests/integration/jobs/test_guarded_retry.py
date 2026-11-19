# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A guarded step's next attempt takes up the run that died, on the step's call log (ADR-089): what
the death owes it rides the step's record (`record.OWED_RUN`) through every park and pace.

The real `execute_step` against real rows, every frame a `FakeFrame`, the real `build()` with a
scripted model (the `test_pause_park` harness). The coder is made guarded for the length of a test,
so the ladder's guarded retry is proven without a browser. A run stopped at a logged point (its
request ceiling) is continued: attempt 2 replays attempt 1, sees its write and one line saying why it
stopped. A run cut inside a call (a provider, the sandbox) is re-entered: the cut request is sent
again, and nothing before it is bought or run twice."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest
from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel

import prismoid.ai.agents.base as agents_base
from prismoid.ai.capabilities.tool_record import OUTCOME_UNKNOWN
from prismoid.ai.gateway import providers
from prismoid.ai.gateway.classes import ModelSpec
from prismoid.conf import settings
from prismoid.db.base import utcnow
from prismoid.db.models.actor import Actor
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.gate import Gate
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.models.usage import UsageEvent
from prismoid.db.session import row_scope, session_scope
from prismoid.errors import Conflict, ModelIncapable, StepFailed
from prismoid.operations import execution, gates, steering
from prismoid.operations import models as model_ops
from prismoid.operations import projects as project_ops
from prismoid.operations import routing as routing_ops
from prismoid.operations import tasks as task_ops
from prismoid.operations.execution import acquire as ex_acquire
from prismoid.operations.execution import harvest as ex_harvest
from prismoid.operations.execution.handlers import rung
from prismoid.schemas.contract import Contract
from prismoid.schemas.events import StepPrompt
from prismoid.schemas.ids import AgentName
from prismoid.schemas.model_class import Family, ModelClass
from prismoid.schemas.outputs import SUBMIT_DOCUMENT, ArtifactType, OutputRef
from prismoid.schemas.plan import GateAction, StepKind, StepSpec, StepState, TaskStatus
from tests.support.cast import member

pytestmark = pytest.mark.integration

Stream = Callable[[list[ModelMessage], AgentInfo], AsyncIterator[str | DeltaToolCalls]]
WRITE = ("write_file", {"path": "/work/memo.md", "content": "EUR 10"})
PUBLISH = ("publish", {"path": "/work/memo.md", "type": "document", "title": "Memo"})


def _spec(title: str, agent: str = "maker") -> dict[str, Any]:
    return StepSpec(
        kind=StepKind.STEP,
        agent=AgentName(agent),
        title=title,
        contract=Contract(
            goal=title,
            acceptance=["it exists"],
            inputs=[],
            output=OutputRef(type=ArtifactType.DOCUMENT, title=title),
        ),
    ).model_dump(mode="json")


async def _running_step(agent: str = "maker") -> tuple[uuid.UUID, uuid.UUID]:
    """Ada's Project task with one READY step of `agent` (the coder). (task, step)"""
    async with session_scope() as s:
        ada, _ = await member(s, "Ada")
        project = (await project_ops.create(s, ada, name=f"p-{uuid.uuid4().hex[:6]}")).project
        task = await task_ops.create_task(s, ada, goal="a memo", project_id=project.id, clarify="never")
        task.transition(TaskStatus.PLANNING)
        step = PlanStep(
            id=uuid.uuid4(),
            task_id=task.id,
            ordinal=0,
            kind=StepKind.STEP.value,
            state=StepState.READY.value,
            title="memo",
            spec=_spec("memo", agent),
            attempt=0,
            epoch=task.plan_epoch,
            turn_epoch=task.turn_epoch,
        )
        s.add(step)
        task.transition(TaskStatus.RUNNING)
        await s.commit()
        return task.id, step.id


async def _run(task_id: uuid.UUID, step_id: uuid.UUID) -> str:
    async with row_scope(Task, task_id) as session:
        return await execution.execute_step(session, task_id, step_id)


async def _walk(task_id: uuid.UUID, step_id: uuid.UUID) -> tuple[str, list[float]]:
    """The step walked the way its `run_step` workflow walks it: each pace (a provider's backoff)
    is waited out - here, only noted - and the ladder entered again with the hand-backs counted.
    (The step's final word, the waits it asked for.)"""
    waits: list[float] = []
    while True:
        async with row_scope(Task, task_id) as session:
            said = await execution.execute_step(session, task_id, step_id, handed_back=len(waits))
        if not said.startswith(execution.PACING):
            return said, waits
        waits.append(float(said.removeprefix(execution.PACING)))


async def _step(task_id: uuid.UUID, step_id: uuid.UUID) -> PlanStep:
    async with row_scope(Task, task_id) as s:
        step = await s.get(PlanStep, step_id)
        assert step is not None
        s.expunge(step)
        return step


async def _prompts(task_id: uuid.UUID) -> list[StepPrompt]:
    async with row_scope(Task, task_id) as s:
        return [e for e in await TaskEvent.replay(s, task_id, since=0) if isinstance(e, StepPrompt)]


def _returns(messages: list[ModelMessage]) -> dict[str, str]:
    return {
        p.tool_call_id: str(p.content)
        for m in messages
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, ToolReturnPart)
    }


def _told(messages: list[ModelMessage]) -> str:
    return "\n".join(
        str(p.content)
        for m in messages
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, UserPromptPart)
    )


def _call(name: str, args: dict[str, Any], call_id: str) -> DeltaToolCalls:
    return {0: DeltaToolCall(name=name, json_args=json.dumps(args), tool_call_id=call_id)}


CEILING = 3  # the guarded coder's request ceiling here: attempt 1 writes, then reads until it stops
READ = ("read_file", {"path": "/work/memo.md"})


def _stops_at_the_ceiling(requests: list[list[ModelMessage]]) -> Stream:
    """Attempt 1 writes the memo, then reads it until its request ceiling stops it. Attempt 2, told
    why it stopped, writes again, publishes, and ends."""

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        requests.append(list(messages))
        returns = _returns(messages)
        if "previous attempt stopped" not in _told(messages):
            if "w1" not in returns:
                yield _call(*WRITE, "w1")
                return
            yield _call(*READ, f"r{len(returns)}")
            return
        if "w2" not in returns:
            yield _call(*WRITE, "w2")
        elif "p1" not in returns:
            yield _call(*PUBLISH, "p1")
        else:
            yield "published the memo"

    return stream


def _cut_once(requests: list[list[ModelMessage]], failure: Exception) -> Stream:
    """Attempt 1 writes the memo, and its next request dies with `failure` once; that request, sent
    again, publishes and ends."""
    cut: list[Exception] = [failure]

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        requests.append(list(messages))
        returns = _returns(messages)
        if "w1" not in returns:
            yield _call(*WRITE, "w1")
        elif cut:
            raise cut.pop()
        elif "p1" not in returns:
            yield _call(*PUBLISH, "p1")
        else:
            yield "published the memo"

    return stream


async def _no_previews(frame: Any, manifest: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    return {}, {}


@pytest.fixture
def real_agent(monkeypatch: pytest.MonkeyPatch) -> Callable[[Stream], None]:
    monkeypatch.setattr(ex_harvest, "extract_previews", _no_previews)

    def use(stream: Stream) -> None:
        monkeypatch.setattr(agents_base, "resolve", lambda *a, **k: FunctionModel(stream_function=stream))

    return use


@pytest.fixture
def guarded_coder(monkeypatch: pytest.MonkeyPatch) -> None:
    """The maker, guarded: the guarded retry proven without a browser."""
    maker = agents_base.get("maker")
    monkeypatch.setitem(agents_base.REGISTRY, maker.name, replace(maker, guarded=True))


@pytest.fixture
def low_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    """A request ceiling attempt 1 meets after its write (`_stops_at_the_ceiling`)."""
    monkeypatch.setattr(settings.worker, "request_ceiling", CEILING)


async def test_a_guarded_retry_continues_the_conversation(
    fake_frames: Any,
    silent_dispatcher: None,
    real_agent: Callable[[Stream], None],
    guarded_coder: None,
    low_ceiling: None,
) -> None:
    task_id, step_id = await _running_step()
    requests: list[list[ModelMessage]] = []
    real_agent(_stops_at_the_ceiling(requests))

    await _run(task_id, step_id)

    step = await _step(task_id, step_id)
    assert step.state == StepState.DONE.value and step.attempt == 2
    assert len(requests) == 2 * CEILING, "attempt 1 replayed, nothing bought twice"
    carried = next(m for m in requests if "Your previous attempt stopped:" in _told(m))
    assert "w1" in _returns(carried), "attempt 1's write rides attempt 2's conversation"
    assert _told(carried).count(_told(requests[0])) == 1, "the contract is told once, never again"
    assert "publish it again" not in _told(carried)
    prompts = await _prompts(task_id)
    assert [p.attempt for p in prompts] == [1, 2]
    assert prompts[1].prompt.startswith("Your previous attempt stopped: the run hit its request ceiling"), (
        "the receipt shows what the continued attempt was told"
    )


async def test_a_guarded_run_cut_inside_a_request_is_re_entered(
    fake_frames: Any, silent_dispatcher: None, real_agent: Callable[[Stream], None], guarded_coder: None
) -> None:
    """A model that answered with nothing usable died inside its request, before the log held it:
    attempt 2 sends that request again, a crash in all but name - no line, nothing before it twice."""
    task_id, step_id = await _running_step()
    requests: list[list[ModelMessage]] = []
    real_agent(_cut_once(requests, UnexpectedModelBehavior("the model answered with nothing usable")))

    await _run(task_id, step_id)

    step = await _step(task_id, step_id)
    assert step.state == StepState.DONE.value and step.attempt == 2
    assert len(requests) == 4, "the write once, the cut request twice, then the end"
    assert _told(requests[2]) == _told(requests[1]), "re-entered: told nothing new"
    assert list(_returns(requests[-1])) == ["w1", "p1"], "the memo was written once"


async def test_a_re_entered_run_on_paper_reads_its_own_draft(
    fake_frames: Any, silent_dispatcher: None, real_agent: Callable[[Stream], None], guarded_coder: None
) -> None:
    """Attempt 1 writes its draft on paper and is cut; attempt 2, on a fresh paper, re-enters the
    run and reads the draft it left - the call log is paper's durable home."""
    task_id, step_id = await _running_step()
    requests: list[list[ModelMessage]] = []
    cut = [UnexpectedModelBehavior("the model answered with nothing usable")]

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        requests.append(list(messages))
        returns = _returns(messages)
        if "w1" not in returns:
            yield _call(*WRITE, "w1")
        elif cut:
            raise cut.pop()
        elif "r1" not in returns:
            yield _call(*READ, "r1")
        elif "p1" not in returns:
            yield _call(*PUBLISH, "p1")
        else:
            yield "published the memo"

    real_agent(stream)

    await _run(task_id, step_id)

    step = await _step(task_id, step_id)
    assert step.state == StepState.DONE.value and step.attempt == 2
    assert "EUR 10" in _returns(requests[-1])["r1"], "attempt 2 read attempt 1's draft"


class _WorkerDied(BaseException):
    """The worker process going down mid-run: nothing after the last commit is recorded."""


async def test_a_worker_dying_on_paper_replays_onto_paper_with_its_draft(
    fake_frames: Any, silent_dispatcher: None, real_agent: Callable[[Stream], None], guarded_coder: None
) -> None:
    """Attempt 1 writes its draft on paper and the worker dies; the recovered run, on a fresh paper,
    replays its call log and publishes that draft without writing it again."""
    task_id, step_id = await _running_step()
    requests: list[list[ModelMessage]] = []
    dies = [_WorkerDied()]

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        requests.append(list(messages))
        returns = _returns(messages)
        if "w1" not in returns:
            yield _call(*WRITE, "w1")
        elif dies:
            raise dies.pop()
        elif "p1" not in returns:
            yield _call(*PUBLISH, "p1")
        else:
            yield "published the memo"

    real_agent(stream)
    with pytest.raises(_WorkerDied):
        await _run(task_id, step_id)

    await _run(task_id, step_id)  # the worker is back: the step's workflow runs the ladder again

    step = await _step(task_id, step_id)
    assert step.state == StepState.DONE.value and step.model_attempts() == 1
    assert list(_returns(requests[-1])) == ["w1", "p1"], "the draft written once, published from paper"
    assert _returns(requests[-1])["p1"].startswith("published"), "the publish found attempt 1's draft"


async def _set_cap(task_id: uuid.UUID, usd: str) -> None:
    async with row_scope(Task, task_id) as s:
        task = await s.get(Task, task_id)
        assert task is not None
        task.budget = {"usd": usd, "tokens": None}
        await s.commit()


async def test_a_steer_taken_during_the_budget_park_reaches_the_continued_attempt(
    fake_frames: Any,
    silent_dispatcher: None,
    real_agent: Callable[[Stream], None],
    guarded_coder: None,
    low_ceiling: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A steer accepted while the step sat on the Bursar's park between
    attempts is applied at the next attempt's start - and a carried attempt, which never hears its
    contract (where that steer lands) again, is told it in its continue line."""
    task_id, step_id = await _running_step()
    await _set_cap(task_id, "1.00")
    open_fresh, spent = rung.open_fresh, []

    async def spend_past_the_cap(ladder: Any) -> str | None:
        # After attempt 1's last call: spend inside a run is the run guard's to brake at a dispatch,
        # so the cap is crossed between attempts, where only the Bursar sees it.
        if ladder.step.attempt != 1 or spent:
            return await open_fresh(ladder)
        spent.append(True)
        async with row_scope(Task, task_id) as s:
            s.add(
                UsageEvent(
                    task_id=task_id,
                    step_id=step_id,
                    provider="anthropic",
                    model="claude-sonnet-5",
                    family="anthropic",
                    units={"request": 50},
                    cost_usd=Decimal("1.50"),
                    at=utcnow(),
                )
            )
            await s.commit()
        return await open_fresh(ladder)

    monkeypatch.setattr(rung, "open_fresh", spend_past_the_cap)
    requests: list[list[ModelMessage]] = []
    real_agent(_stops_at_the_ceiling(requests))

    parked = await _run(task_id, step_id)
    assert parked.startswith(execution.ASKING), "attempt 2 waits on the Bursar"
    async with row_scope(Task, task_id) as s:
        task = await s.get(Task, task_id)
        assert task is not None
        ada = await s.get(Actor, task.creator_actor_id)
        assert ada is not None
        await steering.steer(s, ada, task_id, "Write the totals in euros")
    await _set_cap(task_id, "5.00")
    async with row_scope(Task, task_id) as s:
        ada = await s.get(Actor, ada.id)
        assert ada is not None
        (gate,) = [g for g in await Gate.for_task(s, task_id) if g.is_open()]
        resolution = await gates.resolve(s, ada, gate.id, GateAction.ACCEPT)
    async with row_scope(Task, task_id) as s:  # the run_step workflow's wake, as it stores it
        await execution.store_answer(s, step_id, resolution.model_dump(mode="json"))
        await s.commit()

    await _run(task_id, step_id)

    step = await _step(task_id, step_id)
    assert step.state == StepState.DONE.value and step.attempt == 2
    carried = next(m for m in requests if "Your previous attempt stopped:" in _told(m))
    assert "w1" in _returns(carried), "attempt 2 continued attempt 1's conversation"
    assert "Write the totals in euros" in _told(carried), "the steer reached the carried conversation"
    assert _told(carried).count("Write the totals in euros") == 1
    (_, second) = await _prompts(task_id)
    assert "steered" in second.prompt and "Write the totals in euros" in second.prompt


async def test_a_steer_survives_a_rung_that_never_ran_to_the_continued_attempt(
    fake_frames: Any,
    silent_dispatcher: None,
    real_agent: Callable[[Stream], None],
    guarded_coder: None,
    low_ceiling: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attempt 1 stops at its ceiling (its run continued next); a steer lands; attempt 2 takes it at
    its start and then cannot get its Machine; attempt 3 continues attempt 1's run - and must still be
    told the steer, which attempt 2 applied and no later rung will name again."""
    task_id, step_id = await _running_step()

    open_fresh = rung.open_fresh

    async def steer_before_attempt_2(ladder: Any) -> str | None:
        if ladder.step.attempt == 1:  # between attempt 1's death and attempt 2's start
            async with row_scope(Task, task_id) as s:
                task = await s.get(Task, task_id)
                assert task is not None
                ada = await s.get(Actor, task.creator_actor_id)
                assert ada is not None
                await steering.steer(s, ada, task_id, "Write the totals in euros")
        return await open_fresh(ladder)

    monkeypatch.setattr(rung, "open_fresh", steer_before_attempt_2)
    acquire, calls = ex_acquire.acquire, []

    async def no_frame_on_attempt_2(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == 2:
            raise ModelIncapable("the Machine did not come up", infra=True, machine=True)
        return await acquire(*args, **kwargs)

    monkeypatch.setattr(ex_acquire, "acquire", no_frame_on_attempt_2)
    monkeypatch.setattr(settings.machines, "wait_s", 0)  # the next rung at once, not paced
    requests: list[list[ModelMessage]] = []
    real_agent(_stops_at_the_ceiling(requests))

    await _run(task_id, step_id)

    step = await _step(task_id, step_id)
    assert step.state == StepState.DONE.value
    assert (step.attempt, step.infra_attempts) == (3, 1), "attempt 2 never ran its model"
    carried = next(m for m in requests if "Your previous attempt stopped:" in _told(m))
    assert "w1" in _returns(carried), "attempt 3 continued attempt 1's conversation"
    assert _told(carried).count("Write the totals in euros") == 1, "told the steer, once"
    prompts = await _prompts(task_id)
    assert [p.attempt for p in prompts] == [1, 3]
    assert "Write the totals in euros" in prompts[1].prompt


@pytest.fixture
def two_rows(monkeypatch: pytest.MonkeyPatch) -> Callable[[int], None]:
    """The catalog the guarded coder is served from: claude-a heads the chain, gpt-b follows."""

    def use(window: int = 0) -> None:
        rows = [
            ModelSpec(
                provider=p,
                model_id=m,
                family=Family(p),
                protocol=p,
                classes=(ModelClass.CODE,),
                context_window=window,
            )
            for p, m in (("anthropic", "claude-a"), ("openai", "gpt-b"))
        ]

        async def catalog(session: Any, *, enabled_only: bool = True) -> list[ModelSpec]:
            return rows

        async def order_for(session: Any) -> dict[str, list[str]]:
            return {}

        monkeypatch.setattr(model_ops, "catalog", catalog)
        monkeypatch.setattr(routing_ops, "order_for", order_for)
        monkeypatch.setattr(providers, "api_key_for", lambda provider: "k")

    return use


# ── infra attempts: the weather never spends the model's ladder ───────────────────────────────


def _dies_n_times(requests: list[list[ModelMessage]], failure: Exception, deaths: int) -> Stream:
    """The memo is written once; the request after it dies with `failure` `deaths` times; the one
    after publishes and ends."""
    died: list[int] = []

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        requests.append(list(messages))
        returns = _returns(messages)
        if "w1" not in returns:
            yield _call(*WRITE, "w1")
        elif len(died) < deaths:
            died.append(1)
            raise failure
        elif "p1" not in returns:
            yield _call(*PUBLISH, "p1")
        else:
            yield "published the memo"

    return stream


async def test_overloaded_providers_back_off_and_never_spend_a_model_attempt(
    fake_frames: Any,
    silent_dispatcher: None,
    real_agent: Callable[[Stream], None],
    guarded_coder: None,
    two_rows: Callable[[int], None],
) -> None:
    """Three 529s in a row (each past the transport's own same-model retries): three INFRA
    attempts, each waiting longer on the workflow's clock, each re-entering the one run - and the
    step still finishes on its first model attempt's budget, the task never failed."""
    two_rows(0)
    task_id, step_id = await _running_step()
    requests: list[list[ModelMessage]] = []
    real_agent(_dies_n_times(requests, ModelHTTPError(529, "claude-a", body="overloaded"), deaths=3))

    said, waits = await _walk(task_id, step_id)

    step = await _step(task_id, step_id)
    assert step.state == StepState.DONE.value, said
    base = settings.worker.provider_backoff_s
    assert waits == [base, base * 2, base * 4], "exponential, durable, before every next attempt"
    assert (step.attempt, step.infra_attempts, step.infra_failures) == (4, 3, 3)
    assert step.model_attempts() == 1, "the weather spent none of the model's three"
    assert list(_returns(requests[-1])) == ["w1", "p1"], "one run across every wait: the memo written once"
    async with row_scope(Task, task_id) as s:
        task = await s.get(Task, task_id)
        assert task is not None and task.state != TaskStatus.FAILED.value


async def test_a_step_waiting_out_a_backoff_is_never_replanned_away(
    fake_frames: Any,
    silent_dispatcher: None,
    real_agent: Callable[[Stream], None],
    guarded_coder: None,
    two_rows: Callable[[int], None],
) -> None:
    """A provider death leaves the step `ready` while its workflow sleeps the backoff out. An editor's
    replan in that window must not delete it: the woken workflow would find no step and fail the
    task. The replan is refused like one over a running step, and the step finishes after its wait."""
    two_rows(0)
    task_id, step_id = await _running_step()
    real_agent(_dies_n_times([], ModelHTTPError(529, "claude-a", body="overloaded"), deaths=1))

    async with row_scope(Task, task_id) as session:
        said = await execution.execute_step(session, task_id, step_id, handed_back=0)
    assert said.startswith(execution.PACING) and (await _step(task_id, step_id)).step_state is StepState.READY

    async with row_scope(Task, task_id) as s:
        task = await s.get(Task, task_id)
        assert task is not None
        ada = await s.get(Actor, task.creator_actor_id)
        assert ada is not None
        with pytest.raises(Conflict, match="waiting to try again"):
            await task_ops.replan(s, ada, task_id, reason="start over")

    async with row_scope(Task, task_id) as session:
        await execution.execute_step(session, task_id, step_id, handed_back=1)
    assert (await _step(task_id, step_id)).step_state is StepState.DONE


async def test_the_infra_cap_stops_a_provider_that_never_recovers(
    fake_frames: Any,
    silent_dispatcher: None,
    real_agent: Callable[[Stream], None],
    guarded_coder: None,
    two_rows: Callable[[int], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past `max_infra_attempts` the step escalates with its diagnosis instead of waiting for ever."""
    monkeypatch.setattr(settings.worker, "max_infra_attempts", 2)
    two_rows(0)
    task_id, step_id = await _running_step()
    real_agent(_dies_n_times([], ModelHTTPError(529, "claude-a", body="overloaded"), deaths=99))

    with pytest.raises(StepFailed, match="stopped by the provider or the sandbox"):
        await _walk(task_id, step_id)

    step = await _step(task_id, step_id)
    assert step.state == StepState.ESCALATED.value
    assert (step.infra_failures, step.model_attempts()) == (3, 0)


async def test_a_sandbox_that_stops_answering_mid_tool_is_an_infra_attempt(
    fake_frames: Any,
    silent_dispatcher: None,
    real_agent: Callable[[Stream], None],
    guarded_coder: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `FrameUnreachable` raised inside a tool once escaped the ladder and failed the whole task.
    Now it is one infra attempt: no backoff (the provider is fine), the run re-entered - and the
    build it was in never runs again: its outcome is unknown, and the model is told so."""
    from prismoid.ai.frames.driver import FrameUnreachable

    exec_ = fake_frames.exec

    async def build_dies(self: Any, command: str, timeout: float = 120.0) -> Any:  # noqa: ASYNC109
        if command == "make" and not fake_frames.made_once:
            fake_frames.made_once = True
            raise FrameUnreachable("the frame did not answer exec")
        return await exec_(self, command, timeout)

    monkeypatch.setattr(fake_frames, "made_once", False, raising=False)
    monkeypatch.setattr(fake_frames, "exec", build_dies)
    task_id, step_id = await _running_step()
    requests: list[list[ModelMessage]] = []

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        requests.append(list(messages))
        returns = _returns(messages)
        if "w1" not in returns:
            yield _call(*WRITE, "w1")
        elif "m1" not in returns:
            yield _call("shell", {"command": "make"}, "m1")
        elif "p1" not in returns:
            yield _call(*PUBLISH, "p1")
        else:
            yield "published the memo"

    real_agent(stream)

    said, waits = await _walk(task_id, step_id)

    step = await _step(task_id, step_id)
    assert step.state == StepState.DONE.value, said
    assert waits == [], "a sandbox death waits for nothing"
    assert (step.infra_failures, step.model_attempts()) == (1, 1)
    returns = _returns(requests[-1])
    assert "w1" in returns and returns["m1"] == OUTCOME_UNKNOWN


async def test_the_writer_re_enters_its_run_on_retry(
    fake_frames: Any,
    silent_dispatcher: None,
    real_agent: Callable[[Stream], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The writer as registered - no test makes it guarded: attempt 1 reads its notes and dies of a
    provider failure; attempt 2 re-enters that run and writes the memo, nothing before the cut twice."""
    task_id, step_id = await _running_step("writer")
    requests: list[list[ModelMessage]] = []

    async def notes(self: Any, path: str) -> str:
        return "EUR 10"

    monkeypatch.setattr(fake_frames, "read_file", notes)
    overloaded = [ModelHTTPError(529, "writer-model", body="overloaded")]

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        requests.append(list(messages))
        if "r1" not in _returns(messages):
            yield _call("read_file", {"path": "/work/notes.md"}, "r1")
        elif overloaded:
            raise overloaded.pop()
        else:
            yield _call(SUBMIT_DOCUMENT, {"title": "Memo", "content": "The totals are EUR 10."}, "d1")

    real_agent(stream)

    said, waits = await _walk(task_id, step_id)

    step = await _step(task_id, step_id)
    assert step.state == StepState.DONE.value, said
    assert len(waits) == 1 and step.model_attempts() == 1
    assert "r1" in _returns(requests[-1]) and len(requests) == 3, "the read once, the cut request twice"
