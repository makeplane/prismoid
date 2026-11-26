# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`just evals --suite golden --limit 20` - run the golden cases against the **live system**.

Not a unit test and not a mock. Each case submits a real task through the real API, which plans it,
runs a real model in a real sandbox, publishes a real artifact, verifies it in a fresh frame with a
cross-family judge, retries up to three times, and signs the report. **The score is the fraction
whose signed verdict is `pass`** - because that, and not "did a document appear", is the claim this
product makes.

It costs real money and takes real minutes. That is the correct price for a number you are going to
quote.

The outcome datasets (`SUITES`: `trivial50`, `slack10`, `outcomes`) run through the same door with
`--trials k`: each row is a real task scored on what the user got and what it cost, and the run is
written to `evals/reports/<suite>-<tree>.json`. `--baseline` compares with an earlier report and
`--gate` exits 1 unless the gate passes. `--rows`, `--tag`, `--match`, `--failed-in` and `--sample` run
only some rows (`evals.selection`); a selection is never gated. To add a suite, add one row to `SUITES`; `--dry-run` lists
them all. (`--suite routing` is the skill-routing gate, `evals/suites/routing.py`.)
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import datetime as dt
import functools
import json
import pathlib
import sys
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast

import httpx

from evals import report as report_page
from evals.cases import golden as golden_cases
from evals.cases.golden import CASES, Golden
from evals.evaluators.scores import Scores, score
from evals.metrics import SuiteKind
from evals.runners import Runner, Trial, conversation, decide, not_run, plain_loop, run_trials
from evals.runners import summary as report_file
from evals.runners.live import connected_slugs, setup_gap
from evals.schema import OutcomeRow, load_rows
from evals.selection import OUTSIDE_SAMPLE, Sample, Selection
from evals.suites import RecordingRunner, Scored, documents, research
from prismoid.runner import client as stored
from prismoid.runner import stream
from tools import prompt_evidence

# **This clock starts at submission, so it counts queue time too** - and queue time is a property of
# the harness's own `--concurrency`, not of the system under test. At 420 s it timed out seven cases
# that were merely *waiting*, and scored them alongside real verification failures as though the two
# meant the same thing. A golden case legitimately takes 160–350 s (plan, sandbox, produce, verify in
# a fresh frame, judge cross-family, retry if needed); anything that leaves less than a full case's
# work as slack is measuring the queue, not the product.
TIMEOUT_S = 900.0
POLL_S = 4.0
DATASET_BUDGET_USD = 1.0  # a dataset suite's task cap when `--budget` is not given
BUDGET_NOTE = "the task reached its budget and was cancelled"


@dataclass
class Result:
    case: str
    verdict: str  # pass | fail | unverified | (never delivered) | no receipt | error
    attempts: int
    seconds: float
    missing: tuple[str, ...] = ()
    note: str = ""
    scores: Scores | None = None  # the four evaluators; None if the case never reached a verdict

    @property
    def ok(self) -> bool:
        return self.verdict == "pass" and not self.missing


def api_token() -> str:
    token = stored.current_token()
    if token is None:
        raise SystemExit(f"not logged in to {stored.api_url()} (prismoid login)")
    return token


def _attach(paths: tuple[str, ...]) -> list[dict[str, str]]:
    out = []
    for p in paths:
        data = pathlib.Path(p).read_bytes()
        out.append(
            {
                "filename": pathlib.Path(p).name,
                "content": base64.b64encode(data).decode(),
                "media_type": "text/markdown",
            }
        )
    return out


REPLAY_S = 60.0
REPLAY_MAX_LINES = 100_000


# **A task that FAILS does not emit a `task.failed` frame.** It emits `task.status` carrying a
# terminal status - `task.delivered` is the only bespoke terminal frame there is. Breaking on a frame
# name that the system never emits is the same hang by a different door: the coroutine streams
# forever and holds its semaphore slot. So the replay asks the CLI's own question
# (`stream.task_stopped`) instead of keeping a second copy of the event catalogue.
def is_terminal(line: str) -> bool:
    if not line.startswith("data:"):
        return False
    try:
        payload = json.loads(line.removeprefix("data:"))
    except ValueError:
        return False
    return isinstance(payload, dict) and stream.task_stopped(cast("dict[str, Any]", payload))


async def replay(client: httpx.AsyncClient, task_id: str, *, turns: int = 1) -> list[str]:
    """The task's durable frames from `since=0` to its `turns`-th terminal one; a live stream has no EOF,
    so the reader stops on what it came for, under a wall-clock bound."""
    lines: list[str] = []
    ended = 0

    async def pump() -> None:
        nonlocal ended
        async with client.stream("GET", f"/v1/tasks/{task_id}/events", params={"since": 0}) as s:
            async for line in s.aiter_lines():
                lines.append(line)
                ended += is_terminal(line)
                if ended >= turns or len(lines) >= REPLAY_MAX_LINES:
                    return

    with suppress(TimeoutError):  # a bounded reader returns what it has; it never wedges the run
        await asyncio.wait_for(pump(), timeout=REPLAY_S)
    return lines


async def receipt(client: httpx.AsyncClient, artifact_id: str, *, until: float) -> dict[str, Any] | None:
    """The artifact's signed verification, waited for on the case's clock (it is a 404 until the job
    signs it); None when no receipt came."""
    while True:
        got = await client.get(f"/v1/artifacts/{artifact_id}/verification")
        if got.status_code != 404:
            got.raise_for_status()
            return got.json()
        if time.monotonic() >= until:
            return None
        await asyncio.sleep(POLL_S)


NO_JUDGE = "no judge"


def receipt_note(report: dict[str, Any]) -> str:
    """Who judged, and on a receipt that did not pass, which checks failed: those, not a missing judge,
    are the reason."""
    note = report.get("verifier_family") or NO_JUDGE
    failed = [str(c.get("name")) for c in report.get("checks") or () if c.get("status") == "fail"]
    return f"{note} · failed {', '.join(failed)}" if failed else note


def judged(note: str) -> bool:
    """Did a cross-family judge sign this case (its note leads with the judge's family)?"""
    return bool(note) and not note.startswith(NO_JUDGE)


def missing_terms(terms: tuple[str, ...], content: str, preview: str) -> tuple[str, ...]:
    """The terms a deliverable lacks. A sheet or a deck downloads as zipped bytes no term can be
    found in, so its render sidecar counts as its text too."""
    return tuple(t for t in terms if t not in content and t not in preview)


async def over_budget(client: httpx.AsyncClient, task_id: str) -> bool:
    """Is the task parked on an open budget gate (the Bursar reached its ceiling)?"""
    gates = (await client.get("/v1/gates")).json()
    return any(g["task_id"] == task_id and g["kind"] == "budget" for g in gates)


async def run_case(client: httpx.AsyncClient, case: Golden, budget_usd: float | None = None) -> Result:
    """One golden case as a real task; `budget_usd` caps it, else the deployment's default budget does."""
    started = time.monotonic()
    try:
        payload: dict[str, object] = {"goal": case.goal, "inputs": _attach(case.inputs)}
        if budget_usd is not None:
            payload["budget"] = {"usd": budget_usd}
        if case.bots:
            payload["bots"] = list(case.bots)  # the bot-golden lane pins its subject
        accepted = await client.post("/v1/tasks", json=payload)
        accepted.raise_for_status()
        task_id = accepted.json()["task_id"]

        while time.monotonic() - started < TIMEOUT_S:
            await asyncio.sleep(POLL_S)
            task = (await client.get(f"/v1/tasks/{task_id}")).json()
            if task["status"] in ("delivered", "failed", "cancelled"):
                break
            if task["status"] == "waiting" and await over_budget(client, task_id):
                # The Bursar parked it on a budget gate, which has no clock: only a person ends that
                # wait. Cancel what this harness started and say why, rather than wait out TIMEOUT_S.
                await client.post(f"/v1/tasks/{task_id}/cancel")
                return Result(case.name, "over budget", 0, time.monotonic() - started, note=BUDGET_NOTE)
        else:
            return Result(case.name, "timeout", 0, time.monotonic() - started)

        events = await replay(client, task_id)
        attempts = max(
            (json.loads(line.removeprefix("data: "))["attempt"] for line in events if '"step.state"' in line),
            default=0,
        )

        artifacts = [
            json.loads(line.removeprefix("data: "))["artifact_id"]
            for line in events
            if '"artifact.produced"' in line
        ]
        if not artifacts:
            return Result(case.name, "no artifact", attempts, time.monotonic() - started)

        report = await receipt(client, artifacts[-1], until=started + TIMEOUT_S)
        if report is None:
            return Result(case.name, "no receipt", attempts, time.monotonic() - started)
        body = (await client.get(f"/v1/artifacts/{artifacts[-1]}/download")).json()
        # A plain (unauthenticated) client on purpose: the download URL is presigned, and the API
        # client's Authorization header must not ride along. Threaded so it never blocks the loop.
        content = (await asyncio.to_thread(httpx.get, body["url"], timeout=60, follow_redirects=True)).text

        # The four evaluators. Best-effort and defensive: scoring is a *report*, not a gate, so a
        # missing field or a failed fetch leaves scores=None rather than sinking an otherwise-good case.
        scores = None
        try:
            event_types = [
                json.loads(line.removeprefix("data: ")).get("type", "")
                for line in events
                if line.startswith("data: ")
            ]
            usage = (await client.get(f"/v1/tasks/{task_id}/usage")).json()
            by_family: dict[str, str] = usage.get("by_family", {})
            verifier_family = report.get("verifier_family") or ""
            producer_family = next(
                (
                    f
                    for f in sorted(by_family, key=lambda k: Decimal(str(by_family[k])), reverse=True)
                    if f != verifier_family
                ),
                "",
            )
            budget = task.get("budget") or {}
            budget_usd = Decimal(str(budget["usd"])) if budget.get("usd") is not None else None
            scores = score(
                verdict=report["verdict"],
                verifier_family=verifier_family,
                producer_family=producer_family,
                cost_usd=Decimal(str(usage.get("total_usd", "0"))),
                budget_usd=budget_usd,
                event_types=event_types,
            )
        except Exception:
            scores = None

        preview = await client.get(f"/v1/artifacts/{artifacts[-1]}/preview")
        missing = missing_terms(case.must_contain, content, preview.text if preview.is_success else "")
        return Result(
            case.name,
            report["verdict"],
            attempts,
            time.monotonic() - started,
            missing,
            note=receipt_note(report),
            scores=scores,
        )
    except Exception as exc:
        return Result(case.name, "error", 0, time.monotonic() - started, note=f"{type(exc).__name__}: {exc}")


DATASETS = pathlib.Path(__file__).parent / "datasets"


@dataclass(frozen=True)
class Suite:
    """A dataset of outcome rows run live, as tasks or as conversations: where it is, which gate rule it
    answers to, and how it is paced."""

    dataset: str  # a file under evals/datasets
    kind: SuiteKind  # talk answers in seconds, work runs for minutes: how much slower the gate allows
    timeout_s: float  # one turn's clock, from its submission
    trials: int = 1  # k, when --trials is not given
    concurrency: int = 4  # trials in flight, when --concurrency is not given
    slack: bool = False  # the Slack set's own gate: pass^3 = 1.0, never a Machine, a tool or a second call
    scored: Scored | None = None  # a capability suite: picks the rows run live and scores each turn itself
    chat: bool = False  # rows are lines in a bot conversation, played by the chat runner
    # rows decided in this process: a built agent, its tools stubbed, on the eval seat's models
    decides: Callable[[decide.Seat], Runner] | None = None


SUITES: dict[str, Suite] = {
    "trivial50": Suite("trivial50.jsonl", SuiteKind.TALK, timeout_s=180),
    "slack10": Suite("slack10.jsonl", SuiteKind.TALK, timeout_s=180, trials=3, concurrency=2, slack=True),
    "outcomes": Suite("outcomes.jsonl", SuiteKind.WORK, timeout_s=600, concurrency=6),
    "research": Suite("research.jsonl", SuiteKind.WORK, timeout_s=600, scored=research.SCORED),
    "documents": Suite(
        "documents.jsonl", SuiteKind.WORK, timeout_s=TIMEOUT_S, concurrency=3, scored=documents.SCORED
    ),
    "conversation": Suite(
        "conversation.jsonl", SuiteKind.TALK, timeout_s=180, trials=3, concurrency=2, chat=True
    ),
    "decide": Suite("outcomes.jsonl", SuiteKind.TALK, timeout_s=180, decides=decide.lead),
    "plain-loop": Suite("outcomes.jsonl", SuiteKind.TALK, timeout_s=180, decides=plain_loop.runner),
}


type Gap = Callable[[OutcomeRow, frozenset[str] | None], str | None]


def plan(
    rows: Sequence[OutcomeRow],
    connected: frozenset[str] | None,
    limit: int | None,
    gap: Gap = setup_gap,
    sample: Sample | None = None,
) -> tuple[list[OutcomeRow], dict[str, str]]:
    """The first `limit` rows the runner can run (its `gap` says which), drawn from by `sample`, and why
    every other row is not run."""
    runnable: list[OutcomeRow] = []
    skipped: dict[str, str] = {}
    for row in rows:
        if why := gap(row, connected):
            skipped[row.id] = why
        else:
            runnable.append(row)
    if sample is not None:
        runnable, drawn_out = sample.draw(runnable)
        skipped |= dict.fromkeys(drawn_out, OUTSIDE_SAMPLE)
    if limit is not None:
        skipped |= dict.fromkeys((r.id for r in runnable[limit:]), "beyond --limit")
        runnable = runnable[:limit]
    return runnable, skipped


def _progress(trial: Trial, judged: Callable[[Trial], bool] | None = None) -> None:
    """One line per finished trial, on stderr so `--json` keeps stdout clean."""
    record = report_file.record_of(trial, judged(trial) if judged else None)
    if record is None:
        label = "STALL" if trial.stalled else "SKIP " if trial.incomplete else "ERR  "
        line = f"{label} {trial.row.id} #{trial.index + 1}  {trial.error}"
    else:
        verdict = "ok   " if record.passed else "FAIL "
        ending = "timed out" if record.timed_out else str(record.outcome or "no ending")
        seconds = "-" if record.latency_s is None else f"{record.latency_s:.1f}s"
        line = f"{verdict} {trial.row.id} #{trial.index + 1}  {ending}  ${record.cost_usd:.4f}  {seconds}"
    print(f"  {line}", file=sys.stderr)


type Played = tuple[list[Trial], dict[str, str], Callable[[Trial], bool] | None, str]


async def _played(
    name: str, suite: Suite, rows: list[OutcomeRow], args: argparse.Namespace, gap: Gap, sample: Sample | None
) -> Played:
    """The rows played live through the API: the trials, the rows passed over, a capability suite's
    pass rule and its table."""
    k = args.trials or suite.trials
    async with httpx.AsyncClient(
        base_url=stored.api_url(), headers={"Authorization": f"Bearer {api_token()}"}, timeout=60.0
    ) as client:
        runnable, skipped = plan(rows, await connected_slugs(client), args.limit, gap, sample)
        concurrency = args.concurrency or suite.concurrency
        print(f"── {name}: {len(runnable)} rows x {k}, {concurrency} at a time ──", file=sys.stderr)
        player = conversation.ConversationRunner if suite.chat else RecordingRunner
        runner = player(client, budget_usd=dataset_budget(args), timeout_s=suite.timeout_s)
        trials = await run_trials(runnable, runner, trials=k, concurrency=concurrency, on_done=_progress)
        turns = runner.turns if isinstance(runner, RecordingRunner) else []
        card = None
        if suite.scored:
            until = time.monotonic() + TIMEOUT_S
            card = await suite.scored.score(turns, lambda a: receipt(client, a, until=until))
    verdicts = {id(t.trace): p for t, p in zip(turns, card.passed, strict=True)} if card else {}
    judged = (lambda trial: verdicts.get(id(trial.trace), False)) if card else None
    return trials, skipped, judged, card.table if card else ""


async def _decided(
    name: str, suite: Suite, rows: list[OutcomeRow], args: argparse.Namespace, gap: Gap, sample: Sample | None
) -> Played:
    """The rows decided in this process on the eval seat's models, each held to `decided_well`."""
    if suite.decides is None:
        raise TypeError(f"{name} is not decided in this process")
    k = args.trials or suite.trials
    runnable, skipped = plan(rows, None, args.limit, gap, sample)
    concurrency = args.concurrency or suite.concurrency
    print(f"── {name}: {len(runnable)} rows x {k}, {concurrency} at a time ──", file=sys.stderr)
    runner = suite.decides(await decide.seat())
    done = functools.partial(_progress, judged=decide.decided_well)
    trials, incomplete = not_run(
        await run_trials(runnable, runner, trials=k, concurrency=concurrency, on_done=done)
    )
    return trials, skipped | incomplete, decide.decided_well, ""


async def run_suite(name: str, args: argparse.Namespace) -> int:
    """Run one registered suite, write and print its report, and with `--gate` fail on what the gate fails."""
    suite = SUITES[name]
    if args.gate and not args.baseline and not suite.slack:
        raise SystemExit(f"--gate needs --baseline for {name}: there is nothing to hold it to")
    selection = await asyncio.to_thread(selection_of, name, args)
    if args.gate and selection.partial:
        raise SystemExit(
            f"--gate judges the whole {name} suite, not a selection of its rows: drop --gate "
            "(--baseline still compares the rows both runs scored) or drop the selection flags"
        )
    if args.baseline and not await asyncio.to_thread(pathlib.Path(args.baseline).is_file):
        raise SystemExit(f"{name}: no baseline at {args.baseline}")
    rows = load_rows(DATASETS / suite.dataset)
    rows = suite.scored.select(rows) if suite.scored else rows
    try:
        rows, unselected = selection.pick(rows)
    except ValueError as error:
        raise SystemExit(f"{name}: {error}") from error
    k = args.trials or suite.trials
    gap = conversation.setup_gap if suite.chat else decide.gap if suite.decides else setup_gap
    tree = await asyncio.to_thread(report_file.tree_id)
    if args.dry_run:
        runnable, skipped = plan(rows, None, args.limit, gap, selection.sample)
        print(
            f"{name} @{tree} dry run: {len(runnable)} rows x {k}, "
            f"{len(skipped) + len(unselected)} passed over, nothing submitted"
        )
        if selection.partial:
            print(f"  selected ({len(runnable)}): {', '.join(r.id for r in runnable)}")
        return 0
    play = _decided if suite.decides else _played
    trials, skipped, judged, scorecard = await play(name, suite, rows, args, gap, selection.sample)
    if selection.partial:
        chosen = tuple(dict.fromkeys(t.row.id for t in trials))
        selection = selection.model_copy(update={"chosen": chosen})
    report = report_file.report_of(
        name,
        suite.kind,
        trials,
        k=k,
        tree=tree,
        prompts_sha=await asyncio.to_thread(prompt_evidence.prompts_sha),
        date=dt.datetime.now(dt.UTC).date(),
        not_run=unselected | skipped,
        slack=suite.slack,
        selection=selection if selection.partial else None,
        judged=judged,
        scorecard=scorecard,
    )
    baseline = report_file.load(pathlib.Path(args.baseline)) if args.baseline else None
    if args.gate and baseline is not None and baseline.selection is not None:
        raise SystemExit(f"{name}: --gate needs a whole-suite baseline; {args.baseline} ran a selection")
    report = report.model_copy(update={"gate": report_file.gate_of(report, baseline)})
    saved = await asyncio.to_thread(report_file.save, report)
    if args.report:
        await asyncio.to_thread(pathlib.Path(args.report).write_text, report.model_dump_json(indent=2))
    if args.json:
        print(report.model_dump_json(indent=2))
    else:
        try:
            page = report_page.build(report, baseline)
        except ValueError as error:
            raise SystemExit(f"{name}: {error}") from error
        print(report_page.render_markdown(page))
        if report.scorecard:
            print(report.scorecard)
        print(f"report: {saved}")
    if args.gate and report.gate is not report_file.Gate.PASS:
        reasons = (
            *report_file.gate_failures(report, baseline),
            *report_file.inconclusive_reasons(report, baseline),
        )
        print(f"GATE {report.gate.value.upper()}: " + "; ".join(reasons), file=sys.stderr)
        return 1
    return 0


def dataset_budget(args: argparse.Namespace) -> float:
    """A dataset suite's task cap: `--budget`, else `DATASET_BUDGET_USD`."""
    return DATASET_BUDGET_USD if args.budget is None else args.budget


def selection_of(name: str, args: argparse.Namespace) -> Selection:
    """The selection flags of a dataset run; `--failed-in` resolves to the rows that failed in that report."""
    failed: frozenset[str] = frozenset()
    if args.failed_in:
        try:
            earlier = report_file.load(pathlib.Path(args.failed_in))
        except (OSError, ValueError) as error:
            raise SystemExit(f"{name}: cannot read --failed-in {args.failed_in}: {error}") from error
        if earlier.suite != name:
            raise SystemExit(f"{name}: --failed-in {args.failed_in} is a {earlier.suite} report")
        failed = report_file.failed_rows(earlier)
    if args.seed is not None and args.sample is None:
        raise SystemExit("--seed draws a --sample; give --sample N too")
    return Selection(
        rows=args.rows,
        tags=args.tag,
        match=args.match,
        failed_in=args.failed_in,
        failed=tuple(sorted(failed)),
        sample=None if args.sample is None else Sample(size=args.sample, seed=args.seed or 0),
    )


def _names(text: str) -> tuple[str, ...]:
    """`--rows a,b` / `--tag x,y`: comma-separated names, blanks dropped."""
    return tuple(name for part in text.split(",") if (name := part.strip()))


def _at_least_one(text: str) -> int:
    """`--concurrency`: a whole number of cases at a time, one or more - zero would run nothing."""
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be 1 or more, got {value}")
    return value


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--suite",
        default=None,
        help=f"a dataset suite ({', '.join(SUITES)}), or golden (the default), routing, bot-selection, "
        "bot-golden or skill-golden",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="how many cases (dataset suites: how many runnable rows)"
    )
    parser.add_argument("--concurrency", type=_at_least_one, default=None, help="cases at a time (4)")
    parser.add_argument("--threshold", type=float, default=0.90)
    parser.add_argument("--report", default="", help="Write a JSON report here (for the run-over-run diff).")
    parser.add_argument(
        "--budget",
        type=float,
        default=None,
        help=f"each task's USD cap (dataset suites: {DATASET_BUDGET_USD}; golden: the deployment's default)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="dataset suites: list the rows each would run, submit none (every dataset suite without --suite)",
    )
    parser.add_argument(
        "--trials",
        type=_at_least_one,
        default=None,
        help="dataset suites: times each row is asked (the suite's own k)",
    )
    parser.add_argument(
        "--json", action="store_true", help="dataset suites: print the report as JSON, nothing else"
    )
    parser.add_argument("--baseline", default="", help="dataset suites: an earlier report to compare with")
    parser.add_argument("--gate", action="store_true", help="dataset suites: exit 1 unless the gate passes")
    chooser = parser.add_argument_group("dataset suites: which rows (flags combine with AND)")
    chooser.add_argument("--rows", type=_names, default=(), help="only these row ids: ID[,ID...]")
    chooser.add_argument(
        "--tag",
        type=_names,
        default=(),
        help="rows carrying any of these tags (group, category, variant, expected outcome): TAG[,TAG...]",
    )
    chooser.add_argument(
        "--match", default="", help="a case-insensitive plain substring of the row id, message or label note"
    )
    chooser.add_argument(
        "--failed-in", default="", help="only the rows that failed, errored or timed out in this report"
    )
    chooser.add_argument(
        "--sample", type=_at_least_one, default=None, help="a seeded sample of N runnable rows"
    )
    chooser.add_argument("--seed", type=int, default=None, help="the --sample seed (0)")
    args = parser.parse_args()
    selecting = args.rows or args.tag or args.match or args.failed_in
    if selecting and args.suite not in SUITES:
        raise SystemExit(f"--rows, --tag, --match and --failed-in need --suite, one of: {', '.join(SUITES)}")
    if args.sample is not None and args.suite not in (*SUITES, None, "golden"):
        raise SystemExit(f"--sample needs golden or a dataset suite ({', '.join(SUITES)})")
    if args.seed is not None and args.sample is None:
        raise SystemExit("--seed draws a --sample; give --sample N too")
    if args.dry_run:
        names = [args.suite] if args.suite else list(SUITES)
        if unknown := [name for name in names if name not in SUITES]:
            raise SystemExit(f"--dry-run lists the dataset suites ({', '.join(SUITES)}), not {unknown[0]}")
        for name in names:
            await run_suite(name, args)
        return 0
    args.suite = args.suite or "golden"
    if args.suite in SUITES:
        return await run_suite(args.suite, args)
    args.concurrency = args.concurrency or 4

    # A skill-routing case is one model call, not a task, so it runs before the HTTP client is built.
    if args.suite == "routing":
        from evals.suites import routing

        threshold = 0.95 if args.threshold == 0.90 else args.threshold  # the routing gate is 95%
        return await routing.run(concurrency=args.concurrency, threshold=threshold)

    # The bot analogue: every published bot's selection cases against the live roster,
    # in-process for the same reason routing is. Nightly drift alarm - the publish gate scored
    # each bot against the roster as it stood; this re-scores them against the roster as it IS.
    if args.suite == "bot-selection":
        from evals.suites import bot_selection

        threshold = 0.95 if args.threshold == 0.90 else args.threshold
        return await bot_selection.run(concurrency=args.concurrency, threshold=threshold)

    if args.suite == "bot-golden":
        # The per-bot end-to-end lane: every published bot's `golden:` cases through
        # the same harness - advisory nightly, NEVER the publish gate, for the reason ADR-015
        # recorded for skills: multi-minute real-money runs would kill the sub-minute edit loop.
        from evals.cases.bot_golden import load_cases as load_bot_cases

        cases = (await load_bot_cases())[: args.limit]
        if not cases:
            print("── bot-golden: no published bot ships golden cases yet ──")
            return 0
    elif args.suite == "skill-golden":
        # The per-skill end-to-end lane: every published head's `golden:` cases,
        # through the same harness below - advisory nightly, never the publish gate.
        from evals.cases.skill_golden import load_cases

        cases = (await load_cases())[: args.limit]
        if not cases:
            print("── skill-golden: no published skill ships golden cases yet ──")
            return 0
    else:
        cases = CASES[: args.limit]
        if args.sample is not None:
            cases = golden_cases.sample(cases, args.sample, args.seed or 0)
    gate = asyncio.Semaphore(args.concurrency)

    print(f"── {args.suite}: {len(cases)} cases, {args.concurrency} at a time ──\n")

    async with httpx.AsyncClient(
        base_url=stored.api_url(),
        headers={"Authorization": f"Bearer {api_token()}"},
        timeout=60.0,
    ) as client:

        async def bounded(case: Golden) -> Result:
            async with gate:
                result = await run_case(client, case, args.budget)
                mark = "✓" if result.ok else "✗"
                extra = f"  missing: {', '.join(result.missing)}" if result.missing else ""
                if result.note and not result.ok:
                    extra += f"  ({result.note})"
                print(
                    f"  {mark} {result.case:<24} {result.verdict:<12} "
                    f"attempts={result.attempts}  {result.seconds:5.0f}s{extra}"
                )
                return result

        results = await asyncio.gather(*(bounded(c) for c in cases))

    passed = sum(r.ok for r in results)
    rate = passed / len(results) if results else 0.0
    retried = sum(1 for r in results if r.attempts > 1)

    print()
    print(f"── {passed}/{len(results)} passed  ({rate:.0%})  ·  {retried} needed a retry ──")
    print(f"   judged cross-family: {sum(1 for r in results if judged(r.note))}/{len(results)}")

    # The four evaluators, side by side - they can disagree, and that is the point.
    scored = [r.scores for r in results if r.scores is not None]
    if scored:
        n = len(scored)
        print(
            f"   evaluators - Verified {sum(s.verified for s in scored)}/{n} · "
            f"RubricJudge {sum(s.rubric_judge for s in scored)}/{n} · "
            f"WithinBudget {sum(s.within_budget for s in scored)}/{n} · "
            f"NoInterventions {sum(s.no_interventions for s in scored)}/{n}"
        )

    if args.report:
        # A machine-readable report for the nightly run-over-run diff. Per-case, so the diff can
        # name *which* case regressed - "89% vs 91%" is an alarm; "case revenue-split went pass→fail" is
        # a lead. No timestamp: the CI stamps the run; a wall-clock here would make two identical runs differ.
        report = {
            "suite": args.suite,
            "total": len(results),
            "passed": passed,
            "rate": round(rate, 4),
            "cases": {
                r.case: {
                    "verdict": r.verdict,
                    "ok": r.ok,
                    "attempts": r.attempts,
                    "missing": list(r.missing),
                    "scores": None
                    if r.scores is None
                    else {
                        "verified": r.scores.verified,
                        "rubric_judge": r.scores.rubric_judge,
                        "within_budget": r.scores.within_budget,
                        "no_interventions": r.scores.no_interventions,
                    },
                }
                for r in results
            },
        }
        await asyncio.to_thread(
            pathlib.Path(args.report).write_text, json.dumps(report, indent=2, sort_keys=True)
        )
        print(f"\n   report → {args.report}")

    if rate < args.threshold:
        print(f"\nFAIL: {rate:.0%} is below the {args.threshold:.0%} gate")
        return 1
    print(f"\nPASS: {rate:.0%} ≥ {args.threshold:.0%}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
