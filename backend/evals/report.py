# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The report a contributor reads: what a suite run got, and against a baseline, what regressed.

`runners.summary` holds the run itself (`SuiteReport`) and its numbers; this is the one renderer a
person reads, for a run alone or against a baseline:

* the numbers (accuracy with its interval, the cost-weighted route score, pass^3, cost, latency, shape)
  and, against a baseline, the same numbers before and after with their delta;
* per stratum (the outcome a row expects): rows, accuracy with a bootstrap interval over rows, and the
  paired delta against the baseline with its interval;
* the confusion matrix (expected outcome by delivered outcome) and the costliest misroutes;
* the gate's verdict (`summary.gate_of`, the rule the run itself answers to) with every reason it
  did not pass, and against a baseline the rows that broke or got fixed, named with what each
  delivered.

Two runs are compared only if they are the same suite, and only on the rows both of them scored; the
rows only one run scored are counted and named as such, never folded in.

`python -m evals.report RUN.json [--baseline BASE.json] [--json] [--gate]` prints a saved run; with
`--gate` it exits 0 only on a PASS, 2 when no rule applies (no baseline and no suite rule), else 1.
"""

from __future__ import annotations

import argparse
import datetime as dt
import statistics
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import TypeAdapter

from evals import metrics
from evals.metrics import Estimate, Regression, RoutedTask
from evals.runners import summary as run_file
from evals.runners.summary import Gate, SuiteReport, Summary, TrialError, TrialRecord
from evals.schema import Outcome, Unscored

NO_ENDING = "no ending"
MISROUTES_SHOWN = 15
CHANGES_SHOWN = 25

type Landed = Outcome | Unscored | None


# ── the model ────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Confusion:
    """Trials by the outcome the row expected (rows of the matrix) and the one it got (its cells)."""

    cells: Mapping[Outcome, Mapping[str, int]]


@dataclass(frozen=True)
class Stratum:
    """The rows that expect one outcome. `delta` is the paired change since the baseline, over the rows
    of the stratum that both runs scored; None without a baseline or without such a row."""

    expected: Outcome
    rows: int
    accuracy: Estimate
    delta: Estimate | None = None


@dataclass(frozen=True)
class Misroute:
    row: str
    expected: Outcome
    got: Landed
    cost: int  # the matrix's cost of delivering `got` when `expected` was wanted


@dataclass(frozen=True)
class RowChange:
    """A row that passed in one run and not the other, and what it delivered in each."""

    row: str
    expected: Outcome
    before: Landed
    after: Landed


@dataclass(frozen=True)
class Against:
    """This run against a baseline, on the rows both scored."""

    baseline_tree: str
    baseline_date: dt.date
    before: Summary
    shared: int
    only_before: int
    only_after: int
    accuracy: Estimate  # paired: the mean per-row pass-rate change
    regressions: tuple[Regression, ...]
    broken: tuple[RowChange, ...]
    fixed: tuple[RowChange, ...]


@dataclass(frozen=True)
class RunReport:
    suite: str
    tree: str
    date: dt.date
    gate: Gate  # `summary.gate_of` against the baseline given here
    gate_reasons: tuple[str, ...]  # why the gate did not pass; empty on a pass or with no rule
    summary: Summary
    decided: int  # rows that were scored
    not_run: Mapping[str, int]  # why a row was not scored, with how many
    strata: tuple[Stratum, ...]
    confusion: Confusion
    misroutes: tuple[Misroute, ...]
    replies_sent_to_work: tuple[str, ...]  # a reply row that got a plan: the gate allows none
    errors: tuple[TrialError, ...] = ()  # trials that failed on infra twice: in no score
    stalled: tuple[TrialError, ...] = ()  # trials whose thread stopped before the row's turn: in no score
    retried: int = 0
    against: Against | None = None


# ── building it ──────────────────────────────────────────────────────────────────────────────


def build(
    report: SuiteReport, baseline: SuiteReport | None = None, *, resamples: int = metrics.RESAMPLES
) -> RunReport:
    """Everything the report prints; ValueError for a baseline of another suite or with no shared row."""
    against = None if baseline is None else _against(report, baseline, resamples)
    records = report.records
    return RunReport(
        suite=report.suite,
        tree=report.tree,
        date=report.date,
        gate=run_file.gate_of(report, baseline),
        gate_reasons=(
            *run_file.gate_failures(report, baseline),
            *run_file.inconclusive_reasons(report, baseline),
        ),
        summary=run_file.summarize(report),
        decided=len({r.row for r in records}),
        not_run=dict(Counter(report.not_run.values()).most_common()),
        strata=_strata(records, baseline.records if baseline else None, resamples),
        confusion=_confusion(records),
        misroutes=_misroutes(records),
        replies_sent_to_work=tuple(sorted({r.row for r in records if run_file.reply_sent_to_work(r)})),
        errors=report.errors,
        stalled=report.stalled,
        retried=report.retried,
        against=against,
    )


def _landed(record: TrialRecord) -> str:
    return NO_ENDING if record.outcome is None else record.outcome.value


def _cost(record: TrialRecord) -> int:
    """The misroute cost of one trial: what `metrics.route_score` would charge it."""
    task = RoutedTask(record.dedupe_key, record.expected, frozenset(record.allowed), [record.outcome])
    return round(task.cost())


def _confusion(records: Sequence[TrialRecord]) -> Confusion:
    cells: dict[Outcome, Counter[str]] = defaultdict(Counter)
    for record in records:
        cells[record.expected][_landed(record)] += 1
    return Confusion({outcome: dict(cells[outcome]) for outcome in Outcome if outcome in cells})


def _misroutes(records: Sequence[TrialRecord]) -> tuple[Misroute, ...]:
    missed = [Misroute(r.row, r.expected, r.outcome, cost) for r in records if (cost := _cost(r)) > 0]
    return tuple(sorted(missed, key=lambda m: (-m.cost, m.row)))


def _strata(
    records: Sequence[TrialRecord], baseline: Sequence[TrialRecord] | None, resamples: int
) -> tuple[Stratum, ...]:
    old = None if baseline is None else run_file.passes_by_row(baseline)
    lines: list[Stratum] = []
    for outcome in Outcome:
        mine = run_file.passes_by_row([r for r in records if r.expected is outcome])
        if not mine:
            continue
        delta = None
        if old is not None and mine.keys() & old.keys():
            delta = metrics.paired_delta(mine, old, resamples=resamples)
        lines.append(Stratum(outcome, len(mine), metrics.accuracy(mine, resamples=resamples), delta))
    return tuple(lines)


def _against(report: SuiteReport, baseline: SuiteReport, resamples: int) -> Against:
    if report.suite != baseline.suite:
        raise ValueError(
            f"cannot compare {report.suite!r} with {baseline.suite!r}: two suites hold different rows"
        )
    new, old = run_file.passes_by_row(report.records), run_file.passes_by_row(baseline.records)
    shared = new.keys() & old.keys()
    if not shared:
        raise ValueError(f"the two runs of {report.suite!r} share no row, so there is nothing to compare")
    gate = metrics.regressions(
        run_file.suite_run(report), run_file.suite_run(baseline), report.kind, resamples=resamples
    )
    after_of, before_of = _first_by_row(report.records), _first_by_row(baseline.records)
    changed = sorted(shared)
    mean = {row: statistics.fmean(new[row]) - statistics.fmean(old[row]) for row in changed}

    def changes(rows: Iterable[str]) -> tuple[RowChange, ...]:
        return tuple(
            RowChange(row, after_of[row].expected, before_of[row].outcome, after_of[row].outcome)
            for row in sorted(rows, key=lambda row: (-_cost(after_of[row]), row))
        )

    return Against(
        baseline_tree=baseline.tree,
        baseline_date=baseline.date,
        before=run_file.summarize(baseline),
        shared=len(shared),
        only_before=len(old.keys() - new.keys()),
        only_after=len(new.keys() - old.keys()),
        accuracy=metrics.paired_delta(new, old, resamples=resamples),
        regressions=gate,
        broken=changes(row for row in changed if mean[row] < 0),
        fixed=changes(row for row in changed if mean[row] > 0),
    )


def _first_by_row(records: Sequence[TrialRecord]) -> dict[str, TrialRecord]:
    """One record to speak for each row: the first trial. Rows asked k times show their first outcome."""
    first: dict[str, TrialRecord] = {}
    for record in records:
        first.setdefault(record.row, record)
    return first


# ── the words ────────────────────────────────────────────────────────────────────────────────

DASH = "-"


def _fixed(value: float | None, template: str = "{:.3f}") -> str:
    return DASH if value is None else template.format(value)


def _estimate(value: Estimate | None, *, signed: bool = False) -> str:
    if value is None:
        return DASH
    sign = "+" if signed else ""
    return f"{value.value:{sign}.3f} [{value.low:{sign}.3f}, {value.high:{sign}.3f}]"


def _difference(after: float | None, before: float | None, template: str) -> str:
    return DASH if after is None or before is None else template.format(after - before)


def _numbers(summary: Summary) -> list[tuple[str, str]]:
    return [
        ("rows scored", f"{summary.tasks}x{summary.trials}"),
        ("accuracy", _estimate(summary.accuracy)),
        ("not scored: infra/stalled", f"{summary.errors}/{summary.stalled}"),
        ("route_cw", _estimate(summary.route)),
        ("pass^3", _fixed(summary.pass_hat_3, "{:.2f}")),
        ("$/task", _fixed(summary.usd_per_task, "${:.4f}")),
        ("p50", _fixed(summary.p50_s, "{:.1f}s")),
        ("p95", _fixed(summary.p95_s, "{:.1f}s")),
        ("ttft50", _fixed(summary.ttft_p50_s, "{:.2f}s")),
        ("calls<dlv", _fixed(summary.calls_before_delivery, "{:.2f}")),
        ("tools", _fixed(summary.tool_calls, "{:.2f}")),
        ("mach", _fixed(summary.machines, "{:.2f}")),
        ("cache", _fixed(summary.cache_hit, "{:.0%}")),
        ("undecided checks", str(summary.undecided)),
        ("timeouts", str(summary.timeouts)),
    ]


def _deltas(after: Summary, before: Summary, paired: Estimate) -> dict[str, str]:
    route = (after.route.value if after.route else None, before.route.value if before.route else None)
    return {
        "accuracy": _estimate(paired, signed=True) + " paired",
        "route_cw": _difference(*route, "{:+.3f}"),
        "pass^3": _difference(after.pass_hat_3, before.pass_hat_3, "{:+.2f}"),
        "$/task": _difference(after.usd_per_task, before.usd_per_task, "{:+.4f}"),
        "p50": _difference(after.p50_s, before.p50_s, "{:+.1f}s"),
        "p95": _difference(after.p95_s, before.p95_s, "{:+.1f}s"),
        "ttft50": _difference(after.ttft_p50_s, before.ttft_p50_s, "{:+.2f}s"),
        "calls<dlv": _difference(after.calls_before_delivery, before.calls_before_delivery, "{:+.2f}"),
        "tools": _difference(after.tool_calls, before.tool_calls, "{:+.2f}"),
        "mach": _difference(after.machines, before.machines, "{:+.2f}"),
        "cache": _difference(after.cache_hit, before.cache_hit, "{:+.0%}"),
    }


def _table(head: Sequence[str], rows: Iterable[Sequence[str]]) -> list[str]:
    return [
        f"| {' | '.join(head)} |",
        f"|{'|'.join(' --- ' for _ in head)}|",
        *(f"| {' | '.join(row)} |" for row in rows),
    ]


def _gate_line(report: RunReport) -> str:
    """The verdict first, with every reason it is not a pass."""
    reasons = "; ".join(report.gate_reasons)
    return f"Gate: {report.gate.value}" + (f" ({reasons})" if reasons else "")


def _headline(report: RunReport) -> str:
    against = report.against
    if against is None:
        return "No baseline: nothing to compare with."
    if not against.regressions:
        return f"Regressed: none (against @{against.baseline_tree})"
    names = ", ".join(r.value for r in against.regressions)
    return (
        f"Regressed: {names} (against @{against.baseline_tree}; accuracy "
        f"{_estimate(against.accuracy, signed=True)} paired over {against.shared} shared rows)"
    )


def _numbers_section(report: RunReport) -> list[str]:
    after = _numbers(report.summary)
    if report.against is None:
        return _table(("", "value"), after)
    deltas = _deltas(report.summary, report.against.before, report.against.accuracy)
    before = dict(_numbers(report.against.before))
    return _table(
        ("", f"before @{report.against.baseline_tree}", f"after @{report.tree}", "delta"),
        ((name, before[name], value, deltas.get(name, DASH)) for name, value in after),
    )


def _strata_section(report: RunReport) -> list[str]:
    head = ["expected", "rows", "accuracy (95% CI over rows)"]
    if report.against is not None:
        head.append("paired delta (95% CI)")
    rows = [
        [
            s.expected.value,
            str(s.rows),
            _estimate(s.accuracy),
            *([_estimate(s.delta, signed=True)] if report.against else []),
        ]
        for s in report.strata
    ]
    return _table(head, rows)


def _confusion_section(confusion: Confusion) -> list[str]:
    landed = [o.value for o in Outcome]
    extra = sorted({got for row in confusion.cells.values() for got in row} - set(landed))
    columns = [name for name in (*landed, *extra) if any(name in row for row in confusion.cells.values())]
    rows = [
        [expected.value, *(str(cells.get(name, "")) or "." for name in columns)]
        for expected, cells in confusion.cells.items()
    ]
    return _table(["expected \\ got", *columns], rows)


def _misroute_section(report: RunReport) -> list[str]:
    lines = [
        f"- `{m.row}`: wanted {m.expected.value}, got {m.got.value if m.got else NO_ENDING} (cost {m.cost})"
        for m in report.misroutes[:MISROUTES_SHOWN]
    ]
    if len(report.misroutes) > MISROUTES_SHOWN:
        lines.append(f"- ... {len(report.misroutes) - MISROUTES_SHOWN} more")
    return lines or ["none"]


def _change_lines(changes: Sequence[RowChange]) -> list[str]:
    def got(outcome: Landed) -> str:
        return NO_ENDING if outcome is None else outcome.value

    lines = [
        f"- `{c.row}`: wanted {c.expected.value}, was {got(c.before)}, now {got(c.after)}"
        for c in changes[:CHANGES_SHOWN]
    ]
    if len(changes) > CHANGES_SHOWN:
        lines.append(f"- ... {len(changes) - CHANGES_SHOWN} more")
    return lines


def render_markdown(report: RunReport) -> str:
    """The report as a PR comment: the verdict first, then the evidence."""
    out = [f"# {report.suite} @{report.tree} ({report.date})", ""]
    if report.summary.inconclusive:
        out += [
            f"INCONCLUSIVE: infra errors and stalled threads above {run_file.INCONCLUSIVE_ABOVE:.0%} of the trials",
            "",
        ]
    out += [_gate_line(report), "", _headline(report), ""]
    if report.against is not None:
        against = report.against
        out += [
            f"Compared on {against.shared} shared rows; {against.only_after} only in this run, "
            f"{against.only_before} only in the baseline.",
            "",
        ]
        if against.broken:
            out += [f"## Rows that broke ({len(against.broken)})", *_change_lines(against.broken), ""]
        if against.fixed:
            out += [f"## Rows that got fixed ({len(against.fixed)})", *_change_lines(against.fixed), ""]
    out += ["## Numbers", *_numbers_section(report), ""]
    out += ["## Strata", *_strata_section(report), ""]
    out += ["## Confusion", *_confusion_section(report.confusion), ""]
    if report.replies_sent_to_work:
        out += [
            f"Replies sent to work (the gate allows none): {', '.join(report.replies_sent_to_work)}",
            "",
        ]
    out += ["## Costliest misroutes", *_misroute_section(report), ""]
    if report.not_run:
        out += ["## Not run", *(f"- {count}: {why}" for why, count in report.not_run.items()), ""]
    if report.stalled:
        out += [
            "## Stalled threads (an earlier turn did not deliver; the row's own turn never ran)",
            *(f"- `{e.row}`: {e.error}" for e in report.stalled),
            "",
        ]
    if report.errors or report.retried:
        out += [
            "## Infra",
            *(f"- `{e.row}`: {e.error}" for e in report.errors),
            f"- {report.retried} trial(s) retried once after an infra error",
            "",
        ]
    return "\n".join(out).rstrip() + "\n"


def render_json(report: RunReport) -> str:
    return TypeAdapter(RunReport).dump_json(report, indent=2).decode()


# ── the command ──────────────────────────────────────────────────────────────────────────────


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Print a saved suite run, and against a baseline what regressed."
    )
    parser.add_argument("run", type=Path, help="a report written by a suite run")
    parser.add_argument("--baseline", type=Path, help="an earlier report of the same suite")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument(
        "--gate", action="store_true", help="exit 0 only on a gate pass; 2 when no rule applies; else 1"
    )
    args = parser.parse_args(argv)
    baseline = run_file.load(args.baseline) if args.baseline else None
    try:
        built = build(run_file.load(args.run), baseline)
    except ValueError as error:
        print(f"report: {error}", file=sys.stderr)
        return 2
    print(render_json(built) if args.json else render_markdown(built))
    if not args.gate:
        return 0
    return {Gate.PASS: 0, Gate.UNGATED: 2}.get(built.gate, 1)


if __name__ == "__main__":
    sys.exit(main())
