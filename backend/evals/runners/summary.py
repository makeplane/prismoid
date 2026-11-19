# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A suite run, reduced: the report on disk and the numbers it quotes (`evals.report` prints them).

`SuiteReport` is everything a later run needs to compare against this one (per trial: the outcome the
row wanted and got, the verdict, cost, times, shape), so `--baseline` reads the same file `--json`
writes, and `summarize` is a pure function of it. No trace and no final text is kept: a report can be
committed.

The numbers are accuracy with its interval, pass^3, the cost-weighted route score, $/task, latency
percentiles over every trial, TTFT, shape means, cache hit rate, undecided checks, and the count of
infra errors, stalled threads and timeouts. Infra errors and stalled threads are in no score; a
timeout is, as a turn with no ending, and it never passes.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import math
import statistics
import subprocess
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from evals import metrics
from evals.evaluators import passes
from evals.evaluators.checks import run_checks
from evals.evaluators.outcome import outcome_of
from evals.metrics import Estimate, RoutedTask, SuiteKind, SuiteRun
from evals.runners import Trial
from evals.schema import Outcome, Unscored
from evals.selection import Selection
from evals.trace import PrismoidTrace

REPORTS = Path(__file__).resolve().parent.parent / "reports"
REPO = Path(__file__).resolve().parents[3]
INCONCLUSIVE_ABOVE = 0.10  # more infra errors than this share of the trials and the run proves nothing


class TrialRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    row: str
    dedupe_key: str
    expected: Outcome
    allowed: tuple[Outcome, ...]
    outcome: Outcome | Unscored | None
    passed: bool
    timed_out: bool = False
    undecided: int = 0  # checks that ran and could not decide: neither a pass nor a fail
    cost_usd: float
    latency_s: float | None
    ttft_s: float | None
    calls_before_delivery: int
    tool_calls: int
    machines: int
    cache_read_tokens: int
    prompt_tokens: int


class TrialError(BaseModel):
    model_config = ConfigDict(frozen=True)

    row: str
    error: str


class Gate(StrEnum):
    """The gate's verdict. Only `PASS` passes: a run that proves nothing is `INCONCLUSIVE`, and a run
    with nothing to hold it to (no baseline, no suite rule of its own) is `UNGATED`."""

    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"
    UNGATED = "ungated"


class SuiteReport(BaseModel):
    """One live run of one suite on one tree; `prompts_sha` names the prompts it measured."""

    model_config = ConfigDict(frozen=True)

    suite: str
    kind: SuiteKind
    tree: str
    prompts_sha: str
    gate: Gate = Gate.UNGATED
    slack: bool = False  # held to the Slack set's own rule
    date: dt.date
    trials: int  # k: how many times each row was asked
    records: tuple[TrialRecord, ...]
    errors: tuple[TrialError, ...] = ()  # trials that failed on infra twice: in no score
    stalled: tuple[TrialError, ...] = ()  # trials whose thread stopped before the row's turn: in no score
    retried: int = 0  # trials whose first attempt failed on infra
    not_run: dict[str, str] = {}  # row id -> why this runner could not set it up
    scorecard: str = ""  # a capability suite's own table, one line per scored turn
    selection: Selection | None = None  # the rows a partial run was told to ask; None is the whole suite


def record_of(trial: Trial, passed: bool | None = None) -> TrialRecord | None:
    """The trial as the report keeps it; None for an infra error, which is in no score. `passed` is a
    capability suite's own verdict, in place of the row's checks."""
    trace = trial.trace
    if trace is None:
        return None
    row = trial.row
    return TrialRecord(
        row=row.id,
        dedupe_key=row.dedupe_key,
        expected=_scored(row.expected_outcome),
        allowed=row.allowed_outcomes,
        outcome=outcome_of(row, trace),
        passed=not trial.timed_out and (passes(row, trace) if passed is None else passed),
        timed_out=trial.timed_out,
        undecided=len(run_checks(row, trace).undecided),
        cost_usd=float(trace.cost_usd),
        latency_s=trace.latency_s,
        ttft_s=trace.ttft_s,
        calls_before_delivery=trace.calls_before_delivery,
        tool_calls=len(trace.tool_calls),
        machines=trace.machines,
        cache_read_tokens=trace.tokens.cache_read,
        prompt_tokens=_prompt_tokens(trace),
    )


def report_of(
    suite: str,
    kind: SuiteKind,
    trials: Sequence[Trial],
    *,
    k: int,
    tree: str,
    prompts_sha: str,
    date: dt.date,
    not_run: dict[str, str],
    slack: bool = False,
    judged: Callable[[Trial], bool] | None = None,
    scorecard: str = "",
    selection: Selection | None = None,
) -> SuiteReport:
    """`judged` is a capability suite's own pass rule per trial; `scorecard` its table."""
    records = [record_of(trial, judged(trial) if judged else None) for trial in trials]
    return SuiteReport(
        suite=suite,
        kind=kind,
        slack=slack,
        tree=tree,
        prompts_sha=prompts_sha,
        date=date,
        trials=k,
        records=tuple(r for r in records if r is not None),
        errors=tuple(
            TrialError(row=t.row.id, error=t.error) for t in trials if t.trace is None and not t.stalled
        ),
        stalled=tuple(TrialError(row=t.row.id, error=t.error) for t in trials if t.stalled),
        retried=sum(t.retried for t in trials),
        not_run=not_run,
        scorecard=scorecard,
        selection=selection,
    )


def failed_rows(report: SuiteReport) -> frozenset[str]:
    """Rows with a trial that did not pass: a failed check, a timeout, an infra error or a stall."""
    unscored = (*report.errors, *report.stalled)
    return frozenset(r.row for r in report.records if not r.passed) | {e.row for e in unscored}


def _scored(outcome: Outcome | Unscored) -> Outcome:
    if not isinstance(outcome, Outcome):
        raise ValueError(f"an unscored row ({outcome.value!r}) is never run")
    return outcome


def _prompt_tokens(trace: PrismoidTrace) -> int:
    return trace.tokens.input + trace.tokens.cache_read + trace.tokens.cache_write


# ── the numbers ──────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Summary:
    """The table's cells. None is a number the run could not have: nothing was scored, or too few
    trials for pass^3."""

    tasks: int
    trials: int
    accuracy: Estimate | None = None
    pass_hat_3: float | None = None
    route: Estimate | None = None
    usd_per_task: float | None = None
    p50_s: float | None = None
    p95_s: float | None = None
    ttft_p50_s: float | None = None
    calls_before_delivery: float | None = None
    tool_calls: float | None = None
    machines: float | None = None
    cache_hit: float | None = None
    undecided: int = 0
    errors: int = 0
    stalled: int = 0
    timeouts: int = 0
    inconclusive: bool = False


def passes_by_row(records: Sequence[TrialRecord]) -> dict[str, list[bool]]:
    by_row: dict[str, list[bool]] = defaultdict(list)
    for record in records:
        by_row[record.row].append(record.passed)
    return dict(by_row)


def summarize(report: SuiteReport) -> Summary:
    records = report.records
    unscored = len(report.errors) + len(report.stalled)
    asked = len(records) + unscored
    counted = Summary(
        tasks=len({r.row for r in records}),
        trials=report.trials,
        undecided=sum(r.undecided for r in records),
        errors=len(report.errors),
        stalled=len(report.stalled),
        timeouts=sum(r.timed_out for r in records),
        inconclusive=asked > 0 and unscored / asked > INCONCLUSIVE_ABOVE,
    )
    if not records:
        return counted
    by_row = passes_by_row(records)
    full = {row: passed for row, passed in by_row.items() if len(passed) >= metrics.SLACK_PASS_K}
    latencies = [r.latency_s for r in records if r.latency_s is not None]
    ttfts = [r.ttft_s for r in records if r.ttft_s is not None]
    prompt = sum(r.prompt_tokens for r in records)
    return replace(
        counted,
        accuracy=metrics.accuracy(by_row),
        pass_hat_3=metrics.pass_hat_k(full, metrics.SLACK_PASS_K) if full else None,
        route=_route(records),
        usd_per_task=statistics.fmean(r.cost_usd for r in records),
        p50_s=metrics.nearest_rank(latencies, 50) if latencies else None,
        p95_s=metrics.nearest_rank(latencies, 95) if latencies else None,
        ttft_p50_s=metrics.nearest_rank(ttfts, 50) if ttfts else None,
        calls_before_delivery=statistics.fmean(r.calls_before_delivery for r in records),
        tool_calls=statistics.fmean(r.tool_calls for r in records),
        machines=statistics.fmean(r.machines for r in records),
        cache_hit=sum(r.cache_read_tokens for r in records) / prompt if prompt else None,
    )


def _route(records: Sequence[TrialRecord]) -> Estimate:
    delivered: dict[str, list[Outcome | Unscored | None]] = defaultdict(list)
    first: dict[str, TrialRecord] = {}
    for record in records:
        delivered[record.row].append(record.outcome)
        first.setdefault(record.row, record)
    tasks = [
        RoutedTask(r.dedupe_key, r.expected, frozenset(r.allowed), delivered[row]) for row, r in first.items()
    ]
    return metrics.route_score(tasks)


def suite_run(report: SuiteReport) -> SuiteRun:
    """What the gate compares; a run that delivered nothing reads as infinitely slow."""
    summary = summarize(report)
    return SuiteRun(
        trials=passes_by_row(report.records),
        cost_usd=summary.usd_per_task or 0.0,
        p50_s=math.inf if summary.p50_s is None else summary.p50_s,
        not_run=frozenset(report.not_run),
    )


def reply_sent_to_work(record: TrialRecord) -> bool:
    """A row that wanted a reply got a plan it does not allow: the costliest misroute, which no run may hold."""
    work = (Outcome.PLAN, Outcome.PLAN_WITH_MACHINE)
    return (
        record.expected is Outcome.REPLY and record.outcome in work and record.outcome not in record.allowed
    )


def gate_failures(report: SuiteReport, baseline: SuiteReport | None) -> tuple[str, ...]:
    """Why this run fails: a regression, a reply sent to work, or the Slack set's rule."""
    failures: list[str] = []
    if baseline is not None:
        run, ratchet = suite_run(report), suite_run(baseline)
        for name in metrics.regressions(run, ratchet, report.kind):
            failures.append(f"regressed: {name.value}{_rows_named(name, run, ratchet)}")
    if sent := sorted({r.row for r in report.records if reply_sent_to_work(r)}):
        failures.append(f"replies sent to work: {', '.join(sent)}")
    if report.slack and _slack_broken(report):
        failures.append("slack set: pass^3 below 1.0, or a Machine, a tool or a second call on some trial")
    return tuple(failures)


def inconclusive_reasons(report: SuiteReport, baseline: SuiteReport | None = None) -> tuple[str, ...]:
    """Why this run proves less than it claims: too many unscored trials, a Slack row without a score, or
    baseline rows this run did not run."""
    reasons: list[str] = []
    if summarize(report).inconclusive:
        reasons.append(
            f"inconclusive: infra errors and stalled threads above {INCONCLUSIVE_ABOVE:.0%} of the trials"
        )
    if report.slack and (unscored := sorted(set(report.not_run) | _rows_without_records(report))):
        reasons.append(f"inconclusive: slack rows not scored: {', '.join(unscored)}")
    if baseline is not None:
        scored_before = {r.row for r in baseline.records}
        if skipped := sorted(scored_before & set(report.not_run)):
            reasons.append(f"inconclusive: baseline rows not run here: {', '.join(skipped)}")
    return tuple(reasons)


def _rows_without_records(report: SuiteReport) -> set[str]:
    """Rows asked whose every trial failed on infra or stalled, so nothing of them was scored."""
    asked = {e.row for e in (*report.errors, *report.stalled)}
    return asked - {r.row for r in report.records}


def gate_of(report: SuiteReport, baseline: SuiteReport | None) -> Gate:
    """The gate's verdict on this run. Only a run a rule was applied to, and that proves something, passes."""
    if gate_failures(report, baseline):
        return Gate.FAIL
    if inconclusive_reasons(report, baseline):
        return Gate.INCONCLUSIVE
    return Gate.PASS if baseline is not None or report.slack else Gate.UNGATED


def _rows_named(name: metrics.Regression, run: SuiteRun, ratchet: SuiteRun) -> str:
    rows = {
        metrics.Regression.MISSING: metrics.missing_rows(run, ratchet),
        metrics.Regression.BROKEN: metrics.broken_rows(run, ratchet),
    }.get(name)
    return f" ({', '.join(sorted(rows))})" if rows else ""


def _slack_broken(report: SuiteReport) -> bool:
    if not report.records:
        return True
    by_row = passes_by_row(report.records)
    short = any(len(passed) < metrics.SLACK_PASS_K for passed in by_row.values())
    shape = any(r.machines or r.tool_calls or r.calls_before_delivery > 1 for r in report.records)
    return short or shape or metrics.pass_hat_k(by_row, metrics.SLACK_PASS_K) < 1.0


# ── provenance and the file ──────────────────────────────────────────────────────────────────


def tree_id() -> str:
    """HEAD's tree hash, plus a digest of any working-tree difference, so different code never shares a name."""

    def git(*args: str) -> bytes:
        return subprocess.run(["git", *args], cwd=REPO, check=True, capture_output=True).stdout

    tree = git("rev-parse", "--short=12", "HEAD^{tree}").decode().strip()
    changes = hashlib.sha1(git("diff", "HEAD", "--binary"), usedforsecurity=False)
    for name in sorted(git("ls-files", "-z", "--others", "--exclude-standard").split(b"\0")):
        if name:
            changes.update(name + b"\0" + (REPO / name.decode()).read_bytes())
    clean = changes.digest() == hashlib.sha1(b"", usedforsecurity=False).digest()
    return tree if clean else f"{tree}+{changes.hexdigest()[:8]}"


def save(report: SuiteReport) -> Path:
    """Write the report to `evals/reports/<suite>-<tree>.json`, a selection's beside it under its rows' digest;
    a second run of the same rows on the same tree replaces it."""
    name = f"{report.suite}-{report.tree}"
    if report.selection is not None:
        rows = "\0".join(report.selection.chosen).encode()
        name += f"-selection-{hashlib.sha1(rows, usedforsecurity=False).hexdigest()[:8]}"
    path = REPORTS / f"{name}.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return path


def load(path: Path) -> SuiteReport:
    return SuiteReport.model_validate_json(path.read_text(encoding="utf-8"))
