# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What a turn delivered, and whether the row accepts it - on hand-built event lists, no model, no stack.

Every test builds the durable events a turn would have left, reduces them to a `PrismoidTrace` and asserts
the outcome the user got. None asserts which door fired: `route` and `route_source` are diagnostic, and
`test_the_way_a_turn_was_routed_never_changes_its_outcome` pins that.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from evals.evaluators import checks, passes
from evals.evaluators.budget import budget_verdicts
from evals.evaluators.checks import check_kinds, code_blocks, run_checks, runner_kinds
from evals.evaluators.outcome import outcome_of, route_cost
from evals.schema import (
    Asks,
    CheckKind,
    Outcome,
    OutcomeRow,
    Unscored,
    load_rows,
)
from evals.trace import PrismoidTrace, ToolRecord, events_from_payloads
from prismoid.schemas.events import (
    ArtifactProduced,
    DurableEvent,
    GateOpened,
    MachineCreated,
    MemoryStub,
    MemoryWritten,
    StepNarration,
    TaskClarifying,
    TaskContinued,
    TaskCreated,
    TaskDelivered,
    TaskDequeued,
    TaskPlanned,
    TaskProposed,
    TaskRenamed,
    ToolCalled,
    UsageRecorded,
)
from prismoid.schemas.ids import ArtifactId, GateId, MemoryId, StepId, TaskId
from prismoid.schemas.machine_need import MachineNeed
from prismoid.schemas.model_class import Family
from prismoid.schemas.outputs import ArtifactType
from prismoid.schemas.plan import GateKind, PlanStepView, PlanView, StepKind, StepState, TaskMode
from prismoid.schemas.usage import Unit
from tests.support import journal_names

START = dt.datetime(2026, 11, 14, 9, 0, tzinfo=dt.UTC)


class Log:
    """A durable event log built by hand: a task's events with a rising seq and a ticking clock."""

    def __init__(self) -> None:
        self.task = TaskId(uuid4())
        self.events: list[DurableEvent] = []
        self._seq = 0
        self._now = START

    def _stamp(self, after_s: float) -> tuple[int, dt.datetime]:
        self._seq += 1
        self._now += dt.timedelta(seconds=after_s)
        return self._seq, self._now

    def created(self, goal: str = "do it", connectors_auto: list[str] | None = None) -> Log:
        seq, at = self._stamp(0)
        self.events.append(
            TaskCreated(
                seq=seq,
                task_id=self.task,
                at=at,
                goal=goal,
                mode=TaskMode.STANDARD,
                connectors_auto=connectors_auto or [],
            )
        )
        return self

    def continued(self, turn: int) -> Log:
        seq, at = self._stamp(5)
        self.events.append(TaskContinued(seq=seq, task_id=self.task, at=at, message="and?", turn=turn))
        return self

    def dequeued(self, after_s: float = 0.4) -> Log:
        seq, at = self._stamp(after_s)
        self.events.append(TaskDequeued(seq=seq, task_id=self.task, at=at))
        return self

    def planned(
        self,
        *,
        work: bool,
        machine: MachineNeed | None = None,
        route: str = "standard",
        route_source: str = "planner",
    ) -> Log:
        """A plan of one express step (the turn answers for itself) or one step of work."""
        step = PlanStepView(
            id=StepId(uuid4()),
            title="step",
            kind=StepKind.STEP,
            state=StepState.PENDING,
            lane_role=None if work else "express",
        )
        seq, at = self._stamp(0.2)
        self.events.append(
            TaskPlanned(
                seq=seq,
                task_id=self.task,
                at=at,
                plan=PlanView(steps=[step]),
                machine=machine,
                route=route,
                route_source=route_source,
            )
        )
        return self

    def tool(self, name: str, *, ok: bool = True) -> Log:
        seq, at = self._stamp(0.3)
        self.events.append(ToolCalled(seq=seq, task_id=self.task, at=at, tool=name, ok=ok))
        return self

    def usage(
        self, family: str = "anthropic", cost: str = "0.002", units: dict[Unit, int] | None = None
    ) -> Log:
        seq, at = self._stamp(0.3)
        self.events.append(
            UsageRecorded(
                seq=seq,
                task_id=self.task,
                at=at,
                provider="p",
                model="m",
                family=Family(family),
                units=units or {Unit.REQUEST: 1},
                cost_usd=Decimal(cost),
            )
        )
        return self

    def gate(self, kind: GateKind) -> Log:
        seq, at = self._stamp(0.2)
        self.events.append(
            GateOpened(seq=seq, task_id=self.task, at=at, gate_id=GateId(uuid4()), kind=kind, title="?")
        )
        return self

    def clarifying(self) -> Log:
        seq, at = self._stamp(0.2)
        self.events.append(TaskClarifying(seq=seq, task_id=self.task, at=at, gate_id=GateId(uuid4())))
        return self

    def proposed(self) -> Log:
        seq, at = self._stamp(0.2)
        self.events.append(TaskProposed(seq=seq, task_id=self.task, at=at, goal="a deck"))
        return self

    def machine(self) -> Log:
        seq, at = self._stamp(0.2)
        self.events.append(MachineCreated(seq=seq, task_id=self.task, at=at, profile="base"))
        return self

    def artifact(self, filename: str, role: str = "deliverable") -> Log:
        seq, at = self._stamp(0.2)
        self.events.append(
            ArtifactProduced(
                seq=seq,
                task_id=self.task,
                at=at,
                artifact_id=ArtifactId(uuid4()),
                artifact_type=ArtifactType.SPREADSHEET,
                title="t",
                filename=filename,
                sha256="0" * 64,
                bytes=10,
                role=role,
            )
        )
        return self

    def renamed(self) -> Log:
        seq, at = self._stamp(0.1)
        self.events.append(TaskRenamed(seq=seq, task_id=self.task, at=at, title="Q3 numbers"))
        return self

    def remembered(self, text: str) -> Log:
        seq, at = self._stamp(0.2)
        stub = MemoryStub(id=MemoryId(uuid4()), kind="fact", text=text)
        self.events.append(MemoryWritten(seq=seq, task_id=self.task, at=at, count=1, items=[stub]))
        return self

    def narrated(self, markdown: str) -> Log:
        seq, at = self._stamp(0.2)
        self.events.append(StepNarration(seq=seq, task_id=self.task, at=at, markdown=markdown))
        return self

    def delivered(self, summary: str = "done", after_s: float = 0.5) -> Log:
        seq, at = self._stamp(after_s)
        self.events.append(TaskDelivered(seq=seq, task_id=self.task, at=at, summary=summary))
        return self

    def trace(
        self, *, turn: int = 1, ttft_s: float | None = None, submitted_at: dt.datetime | None = None
    ) -> PrismoidTrace:
        return PrismoidTrace.from_events(self.events, turn=turn, ttft_s=ttft_s, submitted_at=submitted_at)


def reply_turn() -> Log:
    return Log().created().planned(work=False, route="express").usage().delivered("a reply")


# ── the six outcomes, and reject ─────────────────────────────────────────────────────────────


def test_a_turn_that_answers_with_no_tool_is_a_reply() -> None:
    trace = reply_turn().trace()
    assert trace.outcome is Outcome.REPLY
    assert trace.delivered
    assert trace.final_text == "a reply"


def test_a_turn_that_answers_after_reading_the_web_is_light_work() -> None:
    log = Log().created().planned(work=False).tool("web.search").usage().delivered("it is 3.14")
    assert log.trace().outcome is Outcome.LIGHT_WORK


def test_a_recorded_escalation_is_not_work() -> None:
    """A trace from before the Lead may end on the retired answerer's escalation: the turn's ending,
    never work done."""
    log = Log().created().planned(work=False).tool("escalate_to_plan").usage().delivered()
    trace = log.trace()
    assert trace.outcome is Outcome.REPLY
    assert trace.tool_calls == ()


def test_a_turn_that_ran_a_plan_of_work_is_a_plan() -> None:
    log = (
        Log().created().planned(work=True, machine=MachineNeed.FILES).usage().artifact("q3.xlsx").delivered()
    )
    assert log.trace().outcome is Outcome.PLAN


async def test_a_plan_that_ran_a_shell_is_a_plan_with_a_machine() -> None:
    log = Log().created().planned(work=True).machine().tool(await journal_names.shell()).delivered()
    trace = log.trace()
    assert trace.outcome is Outcome.PLAN_WITH_MACHINE
    assert trace.machines == 1


def test_a_plan_that_declared_a_computer_but_drove_none_is_a_plan() -> None:
    log = (
        Log().created().planned(work=True, machine=MachineNeed.COMPUTER).machine()
        .tool("web.search").tool("frame.write_file").tool("frame.publish").delivered()
    )  # fmt: skip
    assert log.trace().outcome is Outcome.PLAN


def test_a_plan_that_drove_a_browser_is_a_plan_with_a_machine_whatever_it_planned() -> None:
    log = Log().created().planned(work=True, machine=MachineNeed.FILES).tool("browser.open").delivered()
    assert log.trace().outcome is Outcome.PLAN_WITH_MACHINE


def test_a_plan_that_only_wrote_files_is_not_a_plan_with_a_machine() -> None:
    log = Log().created().planned(work=True).tool("frame.write_file").tool("frame.publish").delivered()
    assert log.trace().outcome is Outcome.PLAN


@pytest.mark.parametrize("kind", [GateKind.INPUT, GateKind.CONNECT])
def test_a_question_before_any_work_is_an_ask(kind: GateKind) -> None:
    log = Log().created().planned(work=False).gate(kind)
    assert log.trace().outcome is Outcome.ASK


def test_a_plan_whose_first_step_asks_before_doing_anything_is_an_ask() -> None:
    log = Log().created().planned(work=True, machine=MachineNeed.COMPUTER).machine().gate(GateKind.INPUT)
    assert log.trace().outcome is Outcome.ASK


def test_a_question_after_the_work_began_does_not_make_the_plan_an_ask() -> None:
    log = Log().created().planned(work=True).tool("web.search").gate(GateKind.INPUT)
    assert log.trace().outcome is Outcome.PLAN


def test_a_clarifying_round_before_planning_is_an_ask() -> None:
    assert Log().created().clarifying().trace().outcome is Outcome.ASK


def test_an_approval_inside_planned_work_does_not_turn_the_plan_into_an_ask() -> None:
    log = Log().created().planned(work=True).gate(GateKind.APPROVE).gate(GateKind.INPUT).delivered()
    trace = log.trace()
    assert trace.outcome is Outcome.PLAN
    assert trace.gates == (GateKind.APPROVE, GateKind.INPUT)


def test_a_proposal_is_a_propose_and_starts_nothing() -> None:
    trace = Log().created().planned(work=False).usage().proposed().delivered("want a deck?").trace()
    assert trace.outcome is Outcome.PROPOSE
    assert trace.machines == 0


async def test_a_routine_proposal_is_a_propose() -> None:
    log = Log().created().planned(work=False).tool(await journal_names.routine_proposal()).delivered("paused")
    trace = log.trace()
    assert trace.outcome is Outcome.PROPOSE
    assert trace.tool_calls == ()


async def test_a_task_proposal_by_its_tool_is_a_propose() -> None:
    log = Log().created().planned(work=False).tool(await journal_names.task_proposal()).delivered("start?")
    assert log.trace().outcome is Outcome.PROPOSE


async def test_a_refused_proposal_is_no_proposal() -> None:
    log = (
        Log().created().planned(work=False).tool(await journal_names.routine_proposal(), ok=False).delivered()
    )
    assert log.trace().outcome is Outcome.REPLY


def test_a_refused_ask_is_a_reject_with_nothing_spent() -> None:
    trace = PrismoidTrace.refused()
    assert trace.outcome is Unscored.REJECT
    assert not trace.delivered
    assert trace.cost_usd == 0


def test_a_turn_that_ended_with_no_ending_has_no_outcome() -> None:
    assert Log().created().planned(work=False).usage().trace().outcome is None


def test_an_escalated_turn_is_the_plan_it_became() -> None:
    log = Log().created().planned(work=False, route="express").planned(work=True).delivered()
    assert log.trace().outcome is Outcome.PLAN


def test_a_decision_only_run_cut_short_after_the_plan_still_has_its_outcome() -> None:
    assert Log().created().planned(work=True).trace().outcome is Outcome.PLAN


def test_the_way_a_turn_was_routed_never_changes_its_outcome() -> None:
    by_detector = Log().created().planned(work=False, route="express", route_source="detector").delivered()
    by_the_planner = Log().created().planned(work=False, route="standard", route_source="planner").delivered()
    assert by_detector.trace().outcome is by_the_planner.trace().outcome is Outcome.REPLY
    assert by_detector.trace().route_source == "detector"  # read for diagnosis, scored nowhere


# ── turns, ledger, clocks ────────────────────────────────────────────────────────────────────


def test_each_turn_of_a_thread_has_its_own_outcome_and_the_next_knows_the_last_text() -> None:
    log = (
        Log().created().planned(work=False).usage().delivered("the first reply")
        .continued(2).planned(work=False).tool("web.fetch").usage().delivered("the second")
    )  # fmt: skip
    first, second = log.trace(turn=1), log.trace(turn=2)
    assert (first.outcome, second.outcome) == (Outcome.REPLY, Outcome.LIGHT_WORK)
    assert second.prior_text == "the first reply" and first.prior_text == ""
    assert second.calls_before_delivery == 1


def test_asking_for_a_turn_the_events_do_not_hold_is_an_error() -> None:
    with pytest.raises(ValueError, match="turn 2"):
        reply_turn().trace(turn=2)


def test_calls_before_delivery_counts_model_calls_only_and_stops_at_delivery() -> None:
    log = (
        Log().created().planned(work=False)
        .usage().usage().usage("web", units={Unit.SEARCH: 1, Unit.REQUEST: 1}).usage("machine", units={Unit.SECOND: 30})
        .delivered()
        .usage(cost="0.01")  # the verdict, after the answer
    )  # fmt: skip
    trace = log.trace()
    assert trace.calls_before_delivery == 2
    assert trace.cost_usd == Decimal("0.004") + Decimal("0.002") * 2


def test_the_title_call_beside_the_answer_is_not_a_call_the_answer_waited_for() -> None:
    """The namer's usage and its `task.renamed` land in one transaction, so they are adjacent."""
    log = Log().created().usage(cost="0.0001").renamed().planned(work=False).usage().delivered("a reply")
    trace = log.trace()
    assert trace.calls_before_delivery == 1
    assert trace.cost_usd == Decimal("0.0021")  # it is still money the turn spent
    unnamed = Log().created().usage(cost="0.0001").planned(work=False).usage().delivered("a reply")
    assert unnamed.trace().calls_before_delivery == 2


def test_tokens_are_summed_over_model_calls() -> None:
    units = {
        Unit.INPUT_TOKEN: 100,
        Unit.CACHE_READ_TOKEN: 300,
        Unit.CACHE_WRITE_TOKEN: 100,
        Unit.OUTPUT_TOKEN: 50,
    }
    trace = Log().created().planned(work=False).usage(units=units).usage(units=units).delivered().trace()
    assert (trace.tokens.input, trace.tokens.output) == (200, 100)
    assert (trace.tokens.cache_read, trace.tokens.cache_write) == (600, 200)


def test_latency_runs_from_the_clients_submit_when_given_and_the_record_otherwise() -> None:
    log = Log().created().dequeued(0.4).planned(work=False).usage().delivered(after_s=1.0)
    from_the_record = log.trace()
    assert from_the_record.latency_s == pytest.approx(0.4 + 0.2 + 0.3 + 1.0)
    submitted = START - dt.timedelta(seconds=2)
    assert log.trace(submitted_at=submitted).latency_s == pytest.approx(2 + 1.9)
    assert log.trace(ttft_s=0.8).ttft_s == 0.8


def test_artifacts_machines_memory_and_auto_attached_connectors_are_read_off_the_record() -> None:
    log = (
        Log().created(connectors_auto=["slack"]).planned(work=True).machine()
        .artifact("a.xlsx").artifact("outline.md", role="working").remembered("fiscal year starts in April")
        .delivered()
    )  # fmt: skip
    trace = log.trace()
    assert [a.filename for a in trace.artifacts] == ["a.xlsx"]  # working files are scaffolding
    assert trace.memory_written == ("fiscal year starts in April",)
    assert trace.connectors_auto == ("slack",)


def test_the_final_text_falls_back_to_the_last_narration_when_nothing_was_delivered() -> None:
    assert Log().created().narrated("first").narrated("second").trace().final_text == "second"


def test_events_round_trip_through_the_json_the_record_stores() -> None:
    log = reply_turn()
    stored = [event.model_dump(mode="json") for event in log.events]
    assert PrismoidTrace.from_events(events_from_payloads(stored)) == log.trace()


def test_a_payload_that_is_not_a_durable_event_raises() -> None:
    with pytest.raises(ValidationError):
        events_from_payloads([{"type": "task.mystery"}])


# ── rows ─────────────────────────────────────────────────────────────────────────────────────


def row(**fields: object) -> OutcomeRow:
    base: dict[str, object] = {"id": "r1", "message": "hi", "expected_outcome": "reply"}
    return OutcomeRow.model_validate(base | fields)


def test_a_row_that_says_something_outside_the_vocabulary_does_not_load() -> None:
    with pytest.raises(ValidationError):
        row(expected_outcome="plan_with_computer")
    with pytest.raises(ValidationError):
        row(unknown_key=1)
    with pytest.raises(ValidationError):
        row(checks=[{"type": "contains_everything", "value": ["a"]}])


def test_a_reviewed_row_carries_who_why_and_when() -> None:
    reviewed = row(reviewed_by="a reviewer", label_note="a memo fits a message", fresh_as_of="2026-11-14")
    assert (reviewed.reviewed_by, reviewed.label_note) == ("a reviewer", "a memo fits a message")
    assert reviewed.fresh_as_of == dt.date(2026, 11, 14)
    assert row().reviewed_by is None
    with pytest.raises(ValidationError):
        row(fresh_as_of="last Tuesday")


def test_only_a_routing_outcome_is_scored() -> None:
    assert row().scored
    assert not row(expected_outcome="mechanism").scored
    assert not row(expected_outcome="reject").scored


def test_rows_that_are_the_same_ask_share_a_dedupe_key() -> None:
    assert row(id="b", duplicate_of="a").dedupe_key == row(id="a").dedupe_key == "a"


def test_loading_a_file_names_the_line_of_a_bad_row_and_refuses_a_repeated_id(tmp_path: Path) -> None:
    good = json.dumps({"id": "a", "message": "x", "expected_outcome": "ask"})
    path = tmp_path / "rows.jsonl"
    path.write_text(f"{good}\n\n{good}\n")
    with pytest.raises(ValueError, match=r"rows\.jsonl:3: duplicate id 'a'"):
        load_rows(path)
    path.write_text(f"{good}\n" + json.dumps({"id": "b", "message": "x"}) + "\n")
    with pytest.raises(ValueError, match=r"rows\.jsonl:2"):
        load_rows(path)
    path.write_text(f"{good}\n")
    assert [r.id for r in load_rows(path)] == ["a"]


# ── the evaluators ───────────────────────────────────────────────────────────────────────────


def test_the_outcome_is_right_when_it_is_expected_or_allowed_and_a_miss_costs_by_the_matrix() -> None:
    expecting_reply = row(allowed_outcomes=["light_work"])
    assert route_cost(expecting_reply, PrismoidTrace(outcome=Outcome.REPLY)) == 0
    assert route_cost(expecting_reply, PrismoidTrace(outcome=Outcome.LIGHT_WORK)) == 0
    assert route_cost(expecting_reply, PrismoidTrace(outcome=Outcome.PLAN)) == 8
    assert route_cost(expecting_reply, PrismoidTrace(outcome=Outcome.PLAN_WITH_MACHINE)) == 10
    assert route_cost(row(expected_outcome="plan"), PrismoidTrace(outcome=Outcome.REPLY)) == 4


def test_a_turn_with_no_ending_or_a_refusal_costs_the_worst_the_row_could_have_got() -> None:
    assert route_cost(row(), PrismoidTrace()) == 10
    assert route_cost(row(), PrismoidTrace.refused()) == 10


def test_a_row_that_is_not_a_routing_outcome_has_no_cost_and_cannot_pass() -> None:
    mechanism = row(expected_outcome="mechanism")
    assert route_cost(mechanism, PrismoidTrace(outcome=Outcome.REPLY)) is None
    with pytest.raises(ValueError, match="not scored"):
        passes(mechanism, PrismoidTrace(outcome=Outcome.REPLY))


SLACK_ROW = {
    "max_calls_before_delivery": 1,
    "max_tool_calls": 0,
    "max_machines": 0,
    "max_artifacts": 0,
    "budget": {"usd_max": 0.015},
}


def test_a_reply_inside_every_limit_passes_every_budget() -> None:
    trace = reply_turn().trace()
    assert budget_verdicts(row(**SLACK_ROW), trace) == dict.fromkeys(
        ["calls_before_delivery", "tool_calls", "machines", "artifacts", "usd"], True
    )


def test_each_limit_fails_on_its_own() -> None:
    slow_and_dear = (
        Log().created().planned(work=False).usage().usage().tool("web.search").machine()
        .artifact("a.md").usage(cost="0.02").delivered(after_s=9)
    )  # fmt: skip
    assert budget_verdicts(row(**SLACK_ROW), slow_and_dear.trace()) == dict.fromkeys(
        ["calls_before_delivery", "tool_calls", "machines", "artifacts", "usd"], False
    )


def test_a_limit_a_row_does_not_set_is_not_checked() -> None:
    assert budget_verdicts(row(), reply_turn().trace()) == {}


def test_a_trap_row_wants_its_tools_in_range_by_the_name_it_uses() -> None:
    trap = row(expected_outcome="light_work", min_tool_calls=1, expected_tools=["web.fetch"])
    fetched = Log().created().planned(work=False).tool("web.fetch").delivered().trace()
    assert budget_verdicts(trap, fetched) == {"tool_calls": True, "expected_tools": True}
    no_search = Log().created().planned(work=False).tool("web.fetch").delivered().trace()
    no_search_row = row(expected_outcome="light_work", min_tool_calls=1, expected_tools=["web.search"])
    assert budget_verdicts(no_search_row, no_search) == {"tool_calls": True, "expected_tools": False}
    from_memory = Log().created().planned(work=False).delivered().trace()
    assert budget_verdicts(trap, from_memory) == {}  # a reply misses on its outcome, not on the trap's shape
    assert ToolRecord(tool="frame.publish", ok=True).is_named("publish")
    assert ToolRecord(tool="browser.open", ok=True).is_named("browser")
    assert not ToolRecord(tool="browser.open", ok=True).is_named("web")


def test_a_row_may_expect_exactly_one_machine_or_either() -> None:
    one, either = (
        row(expected_outcome="plan", expected_machines=1),
        row(expected_outcome="plan", expected_machines="0-1"),
    )
    with_machine = Log().created().planned(work=True).machine().delivered().trace()
    without = Log().created().planned(work=True).delivered().trace()
    two = Log().created().planned(work=True).machine().machine().delivered().trace()
    assert budget_verdicts(one, with_machine) == {"machines": True}
    assert budget_verdicts(one, without) == {"machines": False}
    assert budget_verdicts(either, with_machine) == budget_verdicts(either, without) == {"machines": True}
    assert budget_verdicts(either, two) == {"machines": False}


def test_an_allowed_outcome_is_held_to_the_caps_and_never_to_the_primary_outcomes_shape() -> None:
    computer = row(
        expected_outcome="plan_with_machine",
        allowed_outcomes=["ask"],
        expected_machines=1,
        min_tool_calls=1,
        expected_tools=["browser"],
        max_tool_calls=3,
    )
    asked = Log().created().planned(work=False).gate(GateKind.INPUT).trace()
    assert asked.outcome is Outcome.ASK
    assert budget_verdicts(computer, asked) == {"tool_calls": True}
    assert passes(computer, asked)
    asked_on_a_machine = Log().created().machine().gate(GateKind.INPUT).trace()
    capped = row(expected_outcome="plan_with_machine", allowed_outcomes=["ask"], max_machines=0)
    assert budget_verdicts(capped, asked_on_a_machine) == {"machines": False}  # a cap holds for any outcome


def test_a_turn_parked_at_a_gate_is_not_held_to_the_tools_its_work_has_yet_to_call() -> None:
    computer = row(expected_outcome="plan_with_machine", min_tool_calls=2, expected_tools=["browser"])
    parked = Log().created().planned(work=True).tool("frame.shell").gate(GateKind.APPROVE).trace()
    assert parked.outcome is Outcome.PLAN_WITH_MACHINE
    assert budget_verdicts(computer, parked) == {}
    no_browser = Log().created().planned(work=True).tool("frame.shell").delivered().trace()
    assert budget_verdicts(computer, no_browser) == {"tool_calls": False, "expected_tools": False}


def answer(text: str, **extra: Any) -> PrismoidTrace:
    return PrismoidTrace(outcome=Outcome.REPLY, delivered=True, final_text=text, **extra)


def checked(spec: Mapping[str, object], text: str, **extra: Any) -> bool | None:
    """True or False when the check decided, None when it could not."""
    report = run_checks(row(checks=[spec]), answer(text, **extra))
    return None if report.undecided else report.passed


@pytest.mark.parametrize(
    ("spec", "good", "bad"),
    [
        ({"type": "contains_all", "value": ["Ravi", "41%"]}, "Ravi owns 41%", "Ravi owns it"),
        ({"type": "contains_any", "value": ["62,985.60", "62985.6"]}, "total 62985.6", "total 63,000"),
        (
            {"type": "contains_in_order", "value": ["auth", "billing"]},
            "auth then billing",
            "billing then auth",
        ),
        ({"type": "lines_equal", "value": ["a", "b"]}, "a\nb\n", "a\nb\nc"),
        ({"type": "lines_equal", "value": ["a", "b"]}, "```\na\nb\n```", "a b"),
        ({"type": "max_words", "value": 3}, "one two three", "one two three four"),
        ({"type": "max_chars", "value": 5}, "12345", "123456"),
        ({"type": "max_sentences", "value": 2}, "One. Two!", "One. Two. Three."),
        ({"type": "max_sentences", "value": 2}, "Costs 3.5 dollars. Fine", "Yes. No. Maybe?"),
        (
            {"type": "max_sentences", "value": 2},
            "1. TCP is reliable.\n2. UDP is not.",
            "1. One.\n2. Two.\n3. Three.",
        ),
        ({"type": "code_block"}, "x\n```py\nprint(1)\n```", "no fence here"),
        ({"type": "min_bullets", "value": 2}, "- a\n- b\n", "- a\n1. b"),
        ({"type": "bullets_exact", "value": 2}, "* a\n• b", "- a\n- b\n- c"),
        ({"type": "numbered_list", "min": 2}, "1. a\n2) b", "1.a\n- b"),
        ({"type": "numbered_or_bullets", "min": 2}, "- a\n- b", "- a\n1. b"),
        ({"type": "slack_mrkdwn"}, "*Done*\n1. a\n2. b <https://x.io|link>", "**Done**"),
        ({"type": "slack_mrkdwn"}, "plain", "# Heading\ntext"),
        ({"type": "slack_mrkdwn"}, "plain", "see [docs](https://x.io)"),
        ({"type": "slack_mrkdwn"}, "plain", "| a | b |\n|---|---|\n| 1 | 2 |"),
        ({"type": "bold", "value": ["Priya"]}, "*Priya* fixes it", "Priya fixes it"),
        ({"type": "cites_source"}, "see https://example.com/x", "as everyone knows"),
    ],
)
def test_text_and_shape_checks_decide_on_the_answer(spec: Mapping[str, object], good: str, bad: str) -> None:
    assert checked(spec, good) is True
    assert checked(spec, bad) is False


def test_a_markdown_table_is_checked_by_its_header_and_its_rows() -> None:
    table = "| Name | City |\n|---|---|\n| Kofi | Accra |\n| Ama | Kumasi |\n\nthanks | all"
    assert checked({"type": "markdown_table", "columns": ["Name", "City"], "rows": 2}, table) is True
    assert checked({"type": "markdown_table", "columns": ["Name", "Town"]}, table) is False
    assert checked({"type": "markdown_table", "rows": 3}, table) is False
    assert checked({"type": "markdown_table"}, "just | a pipe") is False


def test_a_follow_up_is_shorter_than_the_turn_before_it() -> None:
    spec = {"type": "shorter_than_prior"}
    assert checked(spec, "short", prior_text="a much longer first answer") is True
    assert checked(spec, "a much longer second answer", prior_text="short") is False
    assert checked(spec, "short") is None  # the trace holds no earlier turn: neither pass nor fail


async def test_trace_checks_read_tools_machines_artifacts_memory_and_gates() -> None:
    post = journal_names.connector_call("slack", "post_message")
    tools = (ToolRecord(tool="web.search", ok=True), ToolRecord(tool=post, ok=True))
    assert checked({"type": "no_tool_calls"}, "x", tool_calls=tools) is False
    assert checked({"type": "no_tool_calls"}, "x") is True
    assert checked({"type": "tool_called", "value": "web.search"}, "x", tool_calls=tools) is True
    assert checked({"type": "no_fetch"}, "x", tool_calls=tools) is True
    assert checked({"type": "no_machine"}, "x", machines=1) is False
    assert checked({"type": "machine_started_or_gate", "value": True}, "x", gates=(GateKind.APPROVE,)) is True
    assert (
        checked({"type": "memory_written", "contains": "April"}, "x", memory_written=("starts in april",))
        is True
    )
    assert checked({"type": "approval_gate_or_post", "tool": post}, "x", tool_calls=tools) is True
    assert checked({"type": "approval_gate_or_post", "tool": post}, "x") is False
    assert checked({"type": "no_connector_attach"}, "x", connectors_auto=("slack",)) is False


async def test_a_connector_call_is_a_call_to_a_connector_the_turn_held() -> None:
    called = (ToolRecord(tool=journal_names.connector_call("slack", "post_message"), ok=True),)
    attached = row(context={"connectors": ["slack"]}, checks=[{"type": "no_connector_call"}])
    assert not run_checks(attached, answer("x", tool_calls=called)).passed
    assert run_checks(attached, answer("x")).passed
    assert run_checks(attached, answer("x", tool_calls=(ToolRecord(tool="web.search", ok=True),))).passed
    bare = row(checks=[{"type": "no_connector_call"}])
    assert not run_checks(bare, answer("x", tool_calls=called, connectors_auto=("slack",))).passed
    assert run_checks(bare, answer("x", tool_calls=called)).passed  # no connector was attached to call


def test_an_artifact_check_matches_the_extension() -> None:
    assert checked({"type": "artifact", "ext": ".xlsx"}, "x") is False
    produced = Log().created().planned(work=True).artifact("Q3.XLSX").delivered().trace()
    assert run_checks(row(checks=[{"type": "artifact", "ext": ".xlsx"}]), produced).passed
    assert run_checks(row(checks=[{"type": "no_artifact"}]), produced).failed == (CheckKind.NO_ARTIFACT,)


def test_the_checks_a_trace_cannot_decide_are_reported_undecided_not_passed() -> None:
    undecidable = [
        {"type": "language", "value": "es"},
        {"type": "regex_accepts", "pattern_from": "code_block", "accept": ["1"], "reject": ["a"]},
        {"type": "xlsx_cell_equals", "sheet": 0, "cell": "B4", "value": 1},
        {"type": "event", "value": "routine proposed"},
        {"type": "never", "value": "claims it booked"},
        {"type": "asks", "value": "which day"},
    ]
    report = run_checks(row(checks=[*undecidable, {"type": "max_words", "value": 1}]), answer("one two"))
    assert report.failed == (CheckKind.MAX_WORDS,)
    assert len(report.undecided) == len(undecidable)
    assert run_checks(row(checks=undecidable), answer("anything")).passed  # nothing decided, nothing failed


def test_every_check_has_a_runner() -> None:
    specs = check_kinds()
    assert specs == runner_kinds()
    assert {spec.model_fields["type"].default for spec in specs} == set(CheckKind)


def test_code_blocks_are_read_in_order() -> None:
    assert code_blocks("a\n```py\none\n```\nb\n```\ntwo\nthree\n```") == ["one", "two\nthree"]


def test_a_trial_passes_on_outcome_limits_and_decided_checks_together() -> None:
    slack = row(**SLACK_ROW, checks=[{"type": "numbered_list", "min": 2}, {"type": "no_tool_calls"}])
    good = Log().created().planned(work=False).usage().delivered("1. a\n2. b").trace()
    assert passes(slack, good)
    assert not passes(slack, Log().created().planned(work=False).usage().delivered("a, b").trace())
    assert not passes(slack, Log().created().planned(work=True).usage().delivered("1. a\n2. b").trace())
    two_calls = Log().created().planned(work=False).usage().usage().delivered("1. a\n2. b").trace()
    assert not passes(slack, two_calls)


def test_an_empty_reply_fails_a_row_whose_checks_only_cap_its_length() -> None:
    short = row(checks=[{"type": "max_words", "value": 40}, {"type": "language", "value": "es"}])
    assert not passes(short, Log().created().planned(work=False).usage().delivered("").trace())
    assert not passes(short, Log().created().planned(work=False).usage().delivered("  \n").trace())
    assert passes(short, Log().created().planned(work=False).usage().delivered("Hola.").trace())
    looked_up = row(expected_outcome="light_work", checks=[{"type": "max_words", "value": 40}])
    assert not passes(looked_up, Log().created().planned(work=False).tool("web.search").delivered("").trace())


def test_a_turn_that_neither_delivered_nor_stopped_at_a_gate_never_passes() -> None:
    plan_row = row(expected_outcome="plan")
    never_answered = Log().created().planned(work=True).tool("web.search").usage().trace()
    assert never_answered.outcome is Outcome.PLAN  # what it did, for the confusion matrix
    assert not passes(plan_row, never_answered)
    assert passes(plan_row, Log().created().planned(work=True).tool("web.search").delivered().trace())
    asking = row(expected_outcome="ask")
    assert passes(asking, Log().created().planned(work=False).gate(GateKind.INPUT).trace())


def asking(surface: str) -> OutcomeRow:
    return row(
        expected_outcome="propose",
        allowed_outcomes=["ask"],
        context={"surface": surface},
        checks=[{"type": "asks", "value": "which book to order"}],
    )


def test_a_conversation_reply_whose_question_no_judge_has_passed_stays_a_reply() -> None:
    reply = answer("Which book would you like?")
    assert outcome_of(asking("conversation"), reply) is Outcome.REPLY
    assert route_cost(asking("conversation"), reply) == 3


def test_a_conversation_reply_that_passes_its_asks_check_is_scored_as_an_ask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(checks.RUNNERS, Asks, lambda *_: True)  # a judge that passed it
    reply = answer("Which book would you like?")
    assert outcome_of(asking("conversation"), reply) is Outcome.ASK
    assert route_cost(asking("conversation"), reply) == 0
    assert outcome_of(asking("task"), reply) is Outcome.REPLY  # a task's question is a gate, not a reply
    assert outcome_of(asking("conversation"), PrismoidTrace(final_text="x")) is None
