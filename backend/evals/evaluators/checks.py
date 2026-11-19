# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The deterministic checks a row names, run against the turn's trace.

A check reads the model's answer and the trace, never the user's message, and answers with a plain
`str` method: the shape of a list, a table or a Slack message is a few lines of structure, not a
pattern. `RUNNERS` maps each `Check` kind to one function; `test_every_check_has_a_runner` keeps it
in step with `evals.schema`.

A check can come back undecided (`None`): the kinds that need a language detector, a spreadsheet's
bytes, a sandboxed pattern or a judge cannot be decided from a trace. The report counts them on their
own line ("undecided checks") and never folds them into pass or fail, so "could not check" cannot pass for "checked". A judge,
when one is calibrated, replaces the undecided runner of its kind in `RUNNERS`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, get_args

from pydantic import BaseModel

from evals.schema import (
    ApprovalGateOrPost,
    Artifact,
    Asks,
    Bold,
    BulletsExact,
    Check,
    CheckKind,
    CitesSource,
    CodeBlock,
    ContainsAll,
    ContainsAny,
    ContainsInOrder,
    Event,
    Language,
    LinesEqual,
    MachineStartedOrGate,
    MarkdownTable,
    MaxChars,
    MaxSentences,
    MaxWords,
    MemoryWritten,
    MinBullets,
    Never,
    NoArtifact,
    NoConnectorAttach,
    NoConnectorCall,
    NoFetch,
    NoMachine,
    NoToolCalls,
    NumberedList,
    NumberedOrBullets,
    OutcomeRow,
    RegexAccepts,
    ShorterThanPrior,
    SlackMrkdwn,
    ToolCalled,
    XlsxCellEquals,
)
from evals.trace import PrismoidTrace
from prismoid.ai.tools.fetch import TOOL as WEB_FETCH
from prismoid.schemas.plan import GateKind

type Run[S] = Callable[[S, OutcomeRow, PrismoidTrace], bool | None]

RUNNERS: dict[type[BaseModel], Run[Any]] = {}

BULLET_MARKS = ("- ", "* ", "• ")
SENTENCE_ENDS = ".!?。！？"
FENCE = "```"


def runs[S: BaseModel](spec: type[S]) -> Callable[[Run[S]], Run[S]]:
    """Register the function that decides one kind of check."""

    def register(run: Run[S]) -> Run[S]:
        RUNNERS[spec] = run
        return run

    return register


# ── reading the answer ───────────────────────────────────────────────────────────────────────


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _bullets(text: str) -> int:
    return sum(line.startswith(BULLET_MARKS) for line in _lines(text))


def _is_numbered(line: str) -> bool:
    number, _, rest = line.partition(" ")
    stem = number[:-1]
    return bool(rest) and number[-1:] in (".", ")") and stem.isascii() and stem.isdigit()


def _numbered(text: str) -> int:
    return sum(_is_numbered(line) for line in _lines(text))


def _unnumbered(line: str) -> str:
    """The line without its list number, whose dot ends no sentence."""
    return line.partition(" ")[2] if _is_numbered(line) else line


def _sentences(text: str) -> int:
    """Sentence ends are `. ! ?` followed by a space or the end, list numbers aside; a last sentence
    with no mark counts."""
    body = "\n".join(_unnumbered(line) for line in _lines(text))
    ends = sum(
        char in SENTENCE_ENDS and (i + 1 == len(body) or body[i + 1].isspace()) for i, char in enumerate(body)
    )
    return ends + (1 if body and body[-1] not in SENTENCE_ENDS else 0)


def _is_separator(line: str) -> bool:
    cells = _cells(line)
    return all(cell and set(cell) <= set("-:") and "-" in cell for cell in cells)


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip("|").split("|")]


def _table(text: str) -> tuple[list[str], int] | None:
    """The first markdown table: its header cells and its body row count, or None. A table is a
    line of cells, a separator line of dashes, then the rows that still hold a pipe."""
    lines = [line.strip() for line in text.splitlines()]  # blank lines kept: one ends the table
    for i in range(len(lines) - 1):
        if "|" in lines[i] and "|" in lines[i + 1] and _is_separator(lines[i + 1]):
            body = 0
            for line in lines[i + 2 :]:
                if "|" not in line:
                    break
                body += 1
            return _cells(lines[i]), body
    return None


def code_blocks(text: str) -> list[str]:
    """The contents of the fenced blocks, in order."""
    blocks: list[list[str]] = []
    inside = False
    for line in text.splitlines():
        if line.strip().startswith(FENCE):
            inside = not inside
            if inside:
                blocks.append([])
        elif inside:
            blocks[-1].append(line)
    return ["\n".join(block) for block in blocks]


# ── text ─────────────────────────────────────────────────────────────────────────────────────


@runs(ContainsAll)
def check_contains_all(spec: ContainsAll, row: OutcomeRow, trace: PrismoidTrace) -> bool:
    return all(part in trace.final_text for part in spec.value)


@runs(ContainsAny)
def check_contains_any(spec: ContainsAny, row: OutcomeRow, trace: PrismoidTrace) -> bool:
    return any(part in trace.final_text for part in spec.value)


def _in_order(text: str, parts: tuple[str, ...]) -> bool:
    at = 0
    for part in parts:
        found = text.find(part, at)
        if found < 0:
            return False
        at = found + len(part)
    return True


@runs(ContainsInOrder)
def check_contains_in_order(spec: ContainsInOrder, row: OutcomeRow, trace: PrismoidTrace) -> bool:
    return _in_order(trace.final_text, spec.value)


@runs(LinesEqual)
def check_lines_equal(spec: LinesEqual, row: OutcomeRow, trace: PrismoidTrace) -> bool:
    return [line for line in _lines(trace.final_text) if not line.startswith(FENCE)] == list(spec.value)


@runs(MaxWords)
def check_max_words(spec: MaxWords, row: OutcomeRow, trace: PrismoidTrace) -> bool:
    return len(trace.final_text.split()) <= spec.value


@runs(MaxChars)
def check_max_chars(spec: MaxChars, row: OutcomeRow, trace: PrismoidTrace) -> bool:
    return len(trace.final_text) <= spec.value


@runs(MaxSentences)
def check_max_sentences(spec: MaxSentences, row: OutcomeRow, trace: PrismoidTrace) -> bool:
    return _sentences(trace.final_text) <= spec.value


@runs(CodeBlock)
def check_code_block(spec: CodeBlock, row: OutcomeRow, trace: PrismoidTrace) -> bool:
    return bool(code_blocks(trace.final_text))


# ── shape ────────────────────────────────────────────────────────────────────────────────────


@runs(MinBullets)
def check_min_bullets(spec: MinBullets, row: OutcomeRow, trace: PrismoidTrace) -> bool:
    return _bullets(trace.final_text) >= spec.value


@runs(BulletsExact)
def check_bullets_exact(spec: BulletsExact, row: OutcomeRow, trace: PrismoidTrace) -> bool:
    return _bullets(trace.final_text) == spec.value


@runs(NumberedList)
def check_numbered_list(spec: NumberedList, row: OutcomeRow, trace: PrismoidTrace) -> bool:
    return _numbered(trace.final_text) >= spec.min


@runs(NumberedOrBullets)
def check_numbered_or_bullets(spec: NumberedOrBullets, row: OutcomeRow, trace: PrismoidTrace) -> bool:
    return max(_numbered(trace.final_text), _bullets(trace.final_text)) >= spec.min


@runs(MarkdownTable)
def check_markdown_table(spec: MarkdownTable, row: OutcomeRow, trace: PrismoidTrace) -> bool:
    table = _table(trace.final_text)
    if table is None:
        return False
    header, body = table
    return (spec.columns is None or header == list(spec.columns)) and (spec.rows is None or body == spec.rows)


@runs(SlackMrkdwn)
def check_slack_mrkdwn(spec: SlackMrkdwn, row: OutcomeRow, trace: PrismoidTrace) -> bool:
    """Slack's dialect: bold is `*x*`, so no `**`, no `#` headings, no table, no `[text](url)` link."""
    text = trace.final_text
    return (
        "**" not in text
        and "](" not in text
        and not any(line.startswith("#") for line in _lines(text))
        and _table(text) is None
    )


@runs(Bold)
def check_bold(spec: Bold, row: OutcomeRow, trace: PrismoidTrace) -> bool:
    return all(f"*{name}*" in trace.final_text for name in spec.value)


@runs(ShorterThanPrior)
def check_shorter_than_prior(spec: ShorterThanPrior, row: OutcomeRow, trace: PrismoidTrace) -> bool | None:
    if not trace.prior_text:
        return None  # a follow-up check on a trace that holds no earlier turn
    return len(trace.final_text) < len(trace.prior_text)


# ── trace ────────────────────────────────────────────────────────────────────────────────────


@runs(NoToolCalls)
def check_no_tool_calls(spec: NoToolCalls, row: OutcomeRow, trace: PrismoidTrace) -> bool:
    return not trace.tool_calls


@runs(ToolCalled)
def check_tool_called(spec: ToolCalled, row: OutcomeRow, trace: PrismoidTrace) -> bool:
    return any(call.is_named(spec.value) for call in trace.tool_calls)


@runs(NoFetch)
def check_no_fetch(spec: NoFetch, row: OutcomeRow, trace: PrismoidTrace) -> bool:
    return not any(call.tool == WEB_FETCH for call in trace.tool_calls)


@runs(NoConnectorCall)
def check_no_connector_call(spec: NoConnectorCall, row: OutcomeRow, trace: PrismoidTrace) -> bool:
    held = (*row.context.connectors, *trace.connectors_auto)
    return not any(call.calls_a_connector(held) for call in trace.tool_calls)


@runs(NoConnectorAttach)
def check_no_connector_attach(spec: NoConnectorAttach, row: OutcomeRow, trace: PrismoidTrace) -> bool:
    return not trace.connectors_auto


@runs(CitesSource)
def check_cites_source(spec: CitesSource, row: OutcomeRow, trace: PrismoidTrace) -> bool:
    return "http://" in trace.final_text or "https://" in trace.final_text


@runs(NoMachine)
def check_no_machine(spec: NoMachine, row: OutcomeRow, trace: PrismoidTrace) -> bool:
    return trace.machines == 0


@runs(MachineStartedOrGate)
def check_machine_started_or_gate(spec: MachineStartedOrGate, row: OutcomeRow, trace: PrismoidTrace) -> bool:
    return (trace.machines > 0 or bool(trace.gates)) == spec.value


@runs(NoArtifact)
def check_no_artifact(spec: NoArtifact, row: OutcomeRow, trace: PrismoidTrace) -> bool:
    return not trace.artifacts


@runs(Artifact)
def check_artifact(spec: Artifact, row: OutcomeRow, trace: PrismoidTrace) -> bool:
    return any((a.filename or "").casefold().endswith(spec.ext.casefold()) for a in trace.artifacts)


@runs(MemoryWritten)
def check_memory_written(spec: MemoryWritten, row: OutcomeRow, trace: PrismoidTrace) -> bool:
    return any(spec.contains.casefold() in item.casefold() for item in trace.memory_written)


@runs(ApprovalGateOrPost)
def check_approval_gate_or_post(spec: ApprovalGateOrPost, row: OutcomeRow, trace: PrismoidTrace) -> bool:
    return GateKind.APPROVE in trace.gates or any(
        call.ok and call.is_named(spec.tool) for call in trace.tool_calls
    )


def _undecided(spec: BaseModel, row: OutcomeRow, trace: PrismoidTrace) -> None:
    """A language, a cell, a pattern, and a free-text event, `never` or `asks` cannot be read off a trace."""
    return None


RUNNERS.update(dict.fromkeys((Language, RegexAccepts, XlsxCellEquals, Event, Never, Asks), _undecided))


# ── running a row's checks ───────────────────────────────────────────────────────────────────


def check_kinds() -> frozenset[type[BaseModel]]:
    """Every spec the `Check` union can hold: the population `RUNNERS` must cover."""
    return frozenset(get_args(get_args(Check.__value__)[0]))


def runner_kinds() -> frozenset[type[BaseModel]]:
    """Every spec a runner is registered for: it must equal `check_kinds()`."""
    return frozenset(RUNNERS)


@dataclass(frozen=True)
class CheckReport:
    failed: tuple[CheckKind, ...]
    undecided: tuple[CheckKind, ...]

    @property
    def passed(self) -> bool:
        """No decided check failed. An undecided one is neither a pass nor a fail."""
        return not self.failed


def verdict(spec: BaseModel, row: OutcomeRow, trace: PrismoidTrace) -> bool | None:
    """One check's verdict on the trace; None when it cannot be decided from it."""
    return RUNNERS[type(spec)](spec, row, trace)


def run_checks(row: OutcomeRow, trace: PrismoidTrace) -> CheckReport:
    failed: list[CheckKind] = []
    undecided: list[CheckKind] = []
    for spec in row.checks:
        decided = verdict(spec, row, trace)
        if decided is None:
            undecided.append(spec.type)
        elif not decided:
            failed.append(spec.type)
    return CheckReport(tuple(failed), tuple(undecided))
