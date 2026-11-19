# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`--suite <dataset>` end to end against a scripted API: plan, run, report, compare, gate.

The API answers every task with a one-step reply; what is under test is the wiring from a registered
suite to the report on disk and the exit code, not the agent.
"""

from __future__ import annotations

import argparse
import datetime as dt
import functools
import json
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from evals import run
from evals.metrics import SuiteKind
from evals.run import SUITES, Suite, plan
from evals.runners import Trial, summary
from evals.schema import Outcome, OutcomeRow, RowContext
from evals.suites import documents, research
from evals.trace import PrismoidTrace
from prismoid.schemas.events import (
    ArtifactProduced,
    DurableEvent,
    TaskCreated,
    TaskDelivered,
    TaskPlanned,
    UsageRecorded,
)
from prismoid.schemas.ids import ArtifactId, StepId, TaskId
from prismoid.schemas.model_class import Family
from prismoid.schemas.outputs import ArtifactType
from prismoid.schemas.plan import PlanStepView, PlanView, StepKind, StepState, TaskMode
from prismoid.schemas.usage import Unit
from tools import prompt_evidence

TASK = TaskId(uuid4())


def reply_stream(cost: str) -> str:
    """A task's record as the SSE lane sends it: created, planned for one express step, one call, delivered
    two seconds on. Stamped from the real clock, because the runner measures from its own."""
    step = PlanStepView(
        id=StepId(uuid4()), title="s", kind=StepKind.STEP, state=StepState.PENDING, lane_role="express"
    )
    at = dt.datetime.now(dt.UTC)
    events = [
        TaskCreated(seq=1, task_id=TASK, at=at, goal="g", mode=TaskMode.STANDARD),
        TaskPlanned(seq=2, task_id=TASK, at=at, plan=PlanView(steps=[step]), route="express"),
        UsageRecorded(
            seq=3,
            task_id=TASK,
            at=at,
            provider="p",
            model="m",
            family=Family("anthropic"),
            units={Unit.REQUEST: 1},
            cost_usd=Decimal(cost),
        ),
        TaskDelivered(seq=4, task_id=TASK, at=at + dt.timedelta(seconds=2), summary="a reply"),
    ]
    return "".join(f"data: {e.model_dump_json()}\n\n" for e in events)


def api(cost: str) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/connectors":
            return httpx.Response(200, json={"items": [], "next_cursor": None})
        if request.method == "POST":
            return httpx.Response(202, json={"task_id": str(TASK), "last_seq": 1, "turn": 1})
        return httpx.Response(200, text=reply_stream(cost))

    return httpx.MockTransport(handle)


def rows_file(path: Path, count: int, *, odd_ones: int = 0) -> None:
    lines = [{"id": f"t{n}", "message": "m", "expected_outcome": "reply"} for n in range(count)]
    lines += [
        {"id": f"c{n}", "message": "m", "expected_outcome": "reply", "context": {"surface": "conversation"}}
        for n in range(odd_ones)
    ]
    path.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")


def arguments(**overrides: object) -> argparse.Namespace:
    given = {
        "trials": None,
        "limit": None,
        "concurrency": None,
        "budget": 1.0,
        "dry_run": False,
        "json": False,
        "baseline": "",
        "gate": False,
        "report": "",
        "rows": (),
        "tag": (),
        "match": "",
        "failed_in": "",
        "sample": None,
        "seed": None,
    }
    return argparse.Namespace(**(given | overrides))


@pytest.fixture
def stand(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A suite called `trivial50` over a dataset in tmp_path, and reports written there too."""
    rows_file(tmp_path / "t.jsonl", 3, odd_ones=2)
    monkeypatch.setattr(run, "DATASETS", tmp_path)
    monkeypatch.setattr(summary, "REPORTS", tmp_path / "reports")
    monkeypatch.setitem(SUITES, "trivial50", Suite("t.jsonl", SuiteKind.TALK, timeout_s=5, concurrency=2))
    monkeypatch.setattr(run.stored, "api_url", lambda: "http://api")
    monkeypatch.setattr(run, "api_token", lambda: "token")
    return tmp_path


def over(monkeypatch: pytest.MonkeyPatch, cost: str) -> None:
    """Make the suite's HTTP client talk to the scripted API."""
    monkeypatch.setattr(run.httpx, "AsyncClient", functools.partial(httpx.AsyncClient, transport=api(cost)))


# ── registration ─────────────────────────────────────────────────────────────────────────────


def test_the_first_rows_the_runner_can_run_are_taken_and_the_ones_passed_over_say_why() -> None:
    rows = [
        OutcomeRow(
            id="chat", message="m", expected_outcome=Outcome.REPLY, context=RowContext(facts={"turn": 9})
        ),
        OutcomeRow(id="a", message="m", expected_outcome=Outcome.REPLY),
        OutcomeRow(id="b", message="m", expected_outcome=Outcome.REPLY),
        OutcomeRow(id="c", message="m", expected_outcome=Outcome.REPLY),
    ]
    runnable, skipped = plan(rows, frozenset(), limit=2)
    assert [r.id for r in runnable] == ["a", "b"]
    assert (
        skipped["c"] == "beyond --limit"
    )  # a row --limit dropped is said to be not run, never silently absent
    assert list(skipped) == ["chat", "c"]


# ── a dry run ────────────────────────────────────────────────────────────────────────────────


async def test_a_dry_run_says_what_it_would_run_and_submits_nothing(
    stand: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await run.run_suite("trivial50", arguments(dry_run=True, limit=2)) == 0
    out = capsys.readouterr().out
    assert "2 rows x 1" in out and "nothing submitted" in out
    assert not (stand / "reports").exists()  # no client was built: the httpx transport was never patched


def selectable_rows(stand: Path) -> None:
    """Three rows a flag can tell apart: by id, by category, by message or label note."""
    lines = [
        {"id": "t0", "message": "Translate this", "expected_outcome": "reply", "category": "translate"},
        {"id": "t1", "message": "a sum", "expected_outcome": "reply", "category": "arithmetic"},
        {"id": "t2", "message": "m", "expected_outcome": "reply", "label_note": "TRANSLATE a note"},
    ]
    (stand / "t.jsonl").write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")


@pytest.mark.parametrize(
    ("flags", "count", "selected"),
    [
        ({"rows": ("t1",)}, 1, "selected (1): t1"),
        ({"tag": ("translate", "arithmetic")}, 2, "selected (2): t0, t1"),
        ({"match": "translate"}, 2, "selected (2): t0, t2"),
        ({"sample": 2, "seed": 3}, 2, "selected (2): "),
    ],
)
async def test_a_dry_run_shows_exactly_the_selected_rows_and_their_count(
    stand: Path, capsys: pytest.CaptureFixture[str], flags: dict[str, object], count: int, selected: str
) -> None:
    selectable_rows(stand)
    assert await run.run_suite("trivial50", arguments(dry_run=True, **flags)) == 0
    out = capsys.readouterr().out
    assert selected in out
    assert f"dry run: {count} rows x 1, {3 - count} passed over" in out


async def test_a_dry_run_of_failed_in_selects_the_rows_that_failed_there(
    stand: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    errored = summary.report_of(
        "trivial50",
        SuiteKind.TALK,
        [Trial(OutcomeRow(id="t2", message="m", expected_outcome=Outcome.REPLY), 0, None, error="502")],
        k=1,
        tree="old",
        prompts_sha="p" * 64,
        date=dt.date(2026, 11, 1),
        not_run={},
    )
    (stand / "old.json").write_text(errored.model_dump_json(), encoding="utf-8")
    assert await run.run_suite("trivial50", arguments(dry_run=True, failed_in=str(stand / "old.json"))) == 0
    assert "selected (1): t2" in capsys.readouterr().out


async def test_failed_in_a_report_of_another_suite_is_refused(stand: Path) -> None:
    earlier(stand / "docs.json", "0", suite="documents")
    with pytest.raises(SystemExit, match="is a documents report"):
        await run.run_suite("trivial50", arguments(dry_run=True, failed_in=str(stand / "docs.json")))


async def test_an_unknown_row_id_stops_the_run_and_names_it(stand: Path) -> None:
    with pytest.raises(SystemExit, match="trivial50: unknown row id: t9"):
        await run.run_suite("trivial50", arguments(dry_run=True, rows=("t0", "t9")))


# ── a run ────────────────────────────────────────────────────────────────────────────────────


async def test_a_run_scores_what_it_can_run_reports_what_it_cannot_and_writes_the_report(
    stand: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    over(monkeypatch, "0.002")
    assert await run.run_suite("trivial50", arguments(trials=2)) == 0
    shown = capsys.readouterr()
    assert "| rows scored | 3x2 |" in shown.out  # three runnable rows, asked twice
    assert "- 2: conversation rows need the chat lane" in shown.out
    assert shown.err.count("ok   ") == 6
    (written,) = (stand / "reports").glob("trivial50-*.json")
    report = summary.load(written)
    assert len(report.records) == 6
    assert set(report.not_run) == {"c0", "c1"}
    assert all(r.passed and r.cost_usd == pytest.approx(0.002) for r in report.records)


async def test_json_prints_the_report_and_nothing_else(
    stand: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    over(monkeypatch, "0.002")
    await run.run_suite("trivial50", arguments(json=True, limit=1))
    shown = capsys.readouterr()
    assert summary.SuiteReport.model_validate_json(shown.out).suite == "trivial50"


async def test_report_also_writes_the_run_where_it_is_told(
    stand: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    over(monkeypatch, "0.002")
    await run.run_suite("trivial50", arguments(limit=1, report=str(stand / "mine.json")))
    assert summary.load(stand / "mine.json").suite == "trivial50"


# ── the gate ─────────────────────────────────────────────────────────────────────────────────


def earlier(path: Path, cost: str, *, row_id: str = "t0", suite: str = "trivial50") -> Path:
    """A report of a clean reply to `row_id` at `cost`, as an earlier run would have left it."""
    row = OutcomeRow(id=row_id, message="m", expected_outcome=Outcome.REPLY)
    made = PrismoidTrace(
        outcome=Outcome.REPLY, delivered=True, final_text="a reply", cost_usd=Decimal(cost), latency_s=2.0
    )
    report = summary.report_of(
        suite,
        SuiteKind.TALK,
        [Trial(row, 0, made)],
        k=1,
        tree="old",
        prompts_sha="p" * 64,
        date=dt.date(2026, 11, 1),
        not_run={},
    )
    path.write_text(report.model_dump_json(), encoding="utf-8")
    return path


async def test_the_gate_holds_a_run_to_its_baseline_and_exits_one_when_it_is_dearer(
    stand: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    over(monkeypatch, "0.002")
    baseline = earlier(stand / "base.json", "0.001")
    code = await run.run_suite("trivial50", arguments(limit=1, baseline=str(baseline), gate=True))
    shown = capsys.readouterr()
    assert code == 1
    assert "GATE FAIL: regressed: cost" in shown.err
    assert "Regressed: cost (against @old" in shown.out


async def test_a_run_inside_its_baseline_passes_the_gate(
    stand: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    over(monkeypatch, "0.002")
    baseline = earlier(stand / "base.json", "0.002")
    assert await run.run_suite("trivial50", arguments(limit=1, baseline=str(baseline), gate=True)) == 0


async def test_a_gate_with_nothing_to_hold_the_run_to_is_refused_not_passed(stand: Path) -> None:
    with pytest.raises(SystemExit, match="--gate needs --baseline"):
        await run.run_suite("trivial50", arguments(gate=True))


async def test_a_baseline_that_is_not_there_is_named_before_anything_runs(stand: Path) -> None:
    with pytest.raises(SystemExit, match=r"trivial50: no baseline at .*missing\.json"):
        await run.run_suite("trivial50", arguments(baseline=str(stand / "missing.json"), gate=True))


async def test_the_gate_is_refused_on_a_selection_before_anything_runs(stand: Path) -> None:
    baseline = earlier(stand / "base.json", "0.002")
    with pytest.raises(SystemExit, match="--gate judges the whole trivial50 suite"):
        await run.run_suite("trivial50", arguments(rows=("t0",), baseline=str(baseline), gate=True))


async def test_a_selection_records_itself_and_never_overwrites_the_whole_suite_report(
    stand: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    over(monkeypatch, "0.002")
    await run.run_suite("trivial50", arguments())
    await run.run_suite("trivial50", arguments(rows=("t1",)))
    by_selection = {
        bool(report.selection): report
        for report in map(summary.load, (stand / "reports").glob("trivial50-*.json"))
    }
    partial = by_selection[True]
    assert partial.selection is not None and partial.selection.rows == ("t1",)
    assert partial.selection.chosen == ("t1",)
    assert {r.row for r in partial.records} == {"t1"}
    assert partial.not_run["t0"] == "not selected"
    assert by_selection[False].selection is None and len(by_selection[False].records) == 3


async def test_a_selection_is_never_the_baseline_a_gate_holds_a_run_to(
    stand: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    over(monkeypatch, "0.002")
    await run.run_suite("trivial50", arguments(rows=("t0",), report=str(stand / "part.json")))
    with pytest.raises(SystemExit, match="needs a whole-suite baseline"):
        await run.run_suite("trivial50", arguments(baseline=str(stand / "part.json"), gate=True))


# ── what the report records ──────────────────────────────────────────────────────────────────


async def test_a_gated_green_run_records_pass_and_the_prompts_it_measured(
    stand: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    over(monkeypatch, "0.002")
    baseline = earlier(stand / "base.json", "0.002")
    await run.run_suite("trivial50", arguments(limit=1, baseline=str(baseline), gate=True))
    (written,) = (stand / "reports").glob("trivial50-*.json")
    fields = json.loads(written.read_text())
    assert fields["gate"] == "pass"
    assert fields["prompts_sha"] == prompt_evidence.prompts_sha()


async def test_a_run_with_nothing_to_hold_it_to_is_ungated(
    stand: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    over(monkeypatch, "0.002")
    await run.run_suite("trivial50", arguments(limit=1))
    (written,) = (stand / "reports").glob("trivial50-*.json")
    assert json.loads(written.read_text())["gate"] == "ungated"


async def test_a_run_that_failed_the_gate_says_fail_in_its_report(
    stand: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    over(monkeypatch, "0.002")
    baseline = earlier(stand / "base.json", "0.001")
    await run.run_suite("trivial50", arguments(limit=1, baseline=str(baseline), gate=True))
    (written,) = (stand / "reports").glob("trivial50-*.json")
    assert json.loads(written.read_text())["gate"] == "fail"


# ── a capability suite: its own rule is the trial's pass ─────────────────────────────────────

ARTIFACT = ArtifactId(uuid4())


def scored_api(reply: str, *, verdict: str | None = None) -> httpx.MockTransport:
    """The scripted API for one express turn replying `reply`; with `verdict`, it delivers a memo too and
    serves that memo's signed verification, judged by another family."""
    at = dt.datetime.now(dt.UTC)
    step = PlanStepView(
        id=StepId(uuid4()), title="s", kind=StepKind.STEP, state=StepState.PENDING, lane_role="express"
    )
    events: list[DurableEvent] = [
        TaskCreated(seq=1, task_id=TASK, at=at, goal="g", mode=TaskMode.STANDARD),
        TaskPlanned(seq=2, task_id=TASK, at=at, plan=PlanView(steps=[step]), route="express"),
    ]
    if verdict:
        events.append(
            ArtifactProduced(
                seq=3,
                task_id=TASK,
                at=at,
                artifact_id=ARTIFACT,
                artifact_type=ArtifactType.DOCUMENT,
                title="t",
                filename="m.docx",
                sha256="0" * 64,
                bytes=1,
            )
        )
    events.append(TaskDelivered(seq=4, task_id=TASK, at=at + dt.timedelta(seconds=2), summary=reply))
    signed = {
        "verdict": verdict,
        "producer_model": "anthropic:p",
        "producer_family": "anthropic",
        "verifier_model": "openai:j",
        "verifier_family": "openai",
    }

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/connectors":
            return httpx.Response(200, json={"items": [], "next_cursor": None})
        if path == f"/v1/artifacts/{ARTIFACT}/verification":
            return httpx.Response(200, json=signed)
        if request.method == "POST":
            return httpx.Response(202, json={"task_id": str(TASK), "last_seq": 1, "turn": 1})
        return httpx.Response(200, text="".join(f"data: {e.model_dump_json()}\n\n" for e in events))

    return httpx.MockTransport(handle)


def scored_suite(
    stand: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    line: Mapping[str, object],
    api: httpx.MockTransport,
) -> None:
    """Register `name` over a one-row dataset with its own scorer, answered by `api`."""
    (stand / f"{name}.jsonl").write_text(json.dumps(line), encoding="utf-8")
    scorer = {"documents": documents.SCORED, "research": research.SCORED}[name]
    monkeypatch.setitem(SUITES, name, Suite(f"{name}.jsonl", SuiteKind.WORK, timeout_s=5, scored=scorer))
    monkeypatch.setattr(run.httpx, "AsyncClient", functools.partial(httpx.AsyncClient, transport=api))


async def test_a_documents_trial_with_a_signed_fail_is_not_passed_and_fails_the_gate(
    stand: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    memo = {
        "id": "documents:memo-t",
        "message": "m",
        "expected_outcome": "reply",  # the row's checks alone would pass this delivered reply
        "judge_rubric": "1. A memo.\n2. Short.",
        "category": "memo",
    }
    scored_suite(stand, monkeypatch, "documents", memo, scored_api("done", verdict="fail"))
    baseline = earlier(stand / "base.json", "0", row_id="documents:memo-t", suite="documents")
    code = await run.run_suite("documents", arguments(baseline=str(baseline), gate=True))
    shown = capsys.readouterr()
    (written,) = (stand / "reports").glob("documents-*.json")
    report = summary.load(written)
    assert [r.passed for r in report.records] == [False]
    assert "| documents:memo-t | no | fail | other family |" in report.scorecard
    assert summary.gate_of(report, summary.load(baseline)) is summary.Gate.FAIL
    assert (code, report.gate) == (1, summary.Gate.FAIL)
    assert "regressed: broken" in shown.err


async def test_a_research_trial_with_a_wrong_answer_is_not_passed_and_its_table_is_in_the_report(
    stand: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tungsten = {
        "id": "research:t",
        "message": "q",
        "expected_outcome": "reply",
        "checks": [{"type": "contains_any", "value": ["74"]}],  # a substring check alone passes "174"
        "category": "quick_fact",
        "variant": "live",
    }
    scored_suite(stand, monkeypatch, "research", tungsten, scored_api("It is 174."))
    assert await run.run_suite("research", arguments(json=True)) == 0
    report = summary.SuiteReport.model_validate_json(capsys.readouterr().out)
    assert [r.passed for r in report.records] == [False]
    assert "| research:t | no | 0 |" in report.scorecard
