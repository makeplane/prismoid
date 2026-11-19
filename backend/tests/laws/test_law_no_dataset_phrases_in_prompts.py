# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""No case lines: a prompt states a general rule and never carries a phrase from an eval row (the
measurement program; root CLAUDE.md: a prompt line is a rule, not a fix for one failing ask). A phrase
copied from a row makes that row pass for the wrong reason and tells us nothing about the next ask.

Population: every text the model reads, against every ask in the repo. The texts are each agent's prompt
(`tools/prompt_evidence.prompt_texts`) and every tool description under `prismoid/ai/tools` (a function's
docstring, which becomes its tool description, and every `description=` literal), found by AST. The asks
are the rows of the public datasets (`evals/datasets/*.jsonl`), where the retired problem books' rows live.
The held-out twin lives outside the repo and is never read here. An ask is what the person said: the first
paragraph of each of their turns (what follows is pasted material, not the ask). Both sides are case-folded and split into
words. A text fails when it holds any run of 7 words an ask holds, or the whole of an ask of 4 to 6 words
(shorter asks are ordinary words and are not policed).

Fix a failure by stating the rule the example stood for, in words of its own. Never edit a dataset row to
make a prompt pass. `ALLOWED` holds the one known leak, by ask, with the prompts it may sit in and why; an
entry fails once its prompt line or its ask is gone, so it cannot outlive the leak it excuses.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable
from pathlib import Path

from evals.schema import Outcome, OutcomeRow, load_rows
from tests.support.paths import BACKEND
from tools import prompt_evidence

DATASETS = BACKEND / "evals" / "datasets"
TOOLS = BACKEND / "prismoid" / "ai" / "tools"
RUN = 7  # shared words that make a phrase
SHORTEST_ASK = 4  # an ask this short must appear whole to count

type Words = tuple[str, ...]

# ask -> (the prompts that may still carry it, why). Never grown to make a new leak pass.
ALLOWED: dict[str, tuple[frozenset[str], str]] = {}

HOW_TO_FIX = (
    "State the rule the example stood for, in words of its own; a case line teaches the model one row and "
    "nothing else. Never edit a dataset row to make a prompt pass."
)


def words(text: str) -> Words:
    return tuple(re.findall(r"\w+", text.casefold()))


def asks(row: OutcomeRow) -> list[str]:
    """What the person said in `row`: the first paragraph of each of their turns."""
    said = [row.message, *(t.text for t in row.prior_turns if t.role == "user"), *row.context.turns]
    return [turn.split("\n\n")[0] for turn in said]


class Phrases:
    """Every phrase an ask owns: its 7-word runs, or the whole of a short ask."""

    def __init__(self, asked: Iterable[str]) -> None:
        self.runs: set[Words] = set()
        self.whole: set[str] = set()
        for ask in asked:
            tokens = words(ask)
            if len(tokens) >= RUN:
                self.runs.update(tokens[i : i + RUN] for i in range(len(tokens) - RUN + 1))
            elif len(tokens) >= SHORTEST_ASK:
                self.whole.add(" ".join(tokens))

    def shared_with(self, prompt: str) -> list[str]:
        """The phrases of an ask that `prompt` repeats."""
        tokens = words(prompt)
        found = [
            " ".join(tokens[i : i + RUN])
            for i in range(len(tokens) - RUN + 1)
            if tokens[i : i + RUN] in self.runs
        ]
        padded = f" {' '.join(tokens)} "
        return found + [phrase for phrase in sorted(self.whole) if f" {phrase} " in padded]


def dataset_asks() -> list[str]:
    paths = sorted(DATASETS.glob("*.jsonl"))
    assert paths, f"no datasets under {DATASETS}: the law would pass over nothing"
    return [ask for path in paths for row in load_rows(path) for ask in asks(row)]


def _literal(node: ast.expr) -> str | None:
    """A string literal, implicitly concatenated or not; None for anything computed."""
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def tool_texts(root: Path = TOOLS) -> dict[str, str]:
    """Every tool description under `root`: function docstrings and `description=` literals, by place."""
    texts: dict[str, str] = {}
    for path in sorted(root.rglob("*.py")):
        name = path.relative_to(root.parent).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and (doc := ast.get_docstring(node)):
                texts[f"{name}:{node.lineno}"] = doc
            elif (
                isinstance(node, ast.keyword) and node.arg == "description" and (text := _literal(node.value))
            ):
                texts[f"{name}:{node.value.lineno}"] = text
    return texts


def problems(prompts: dict[str, str], phrases: Phrases) -> list[str]:
    out: list[str] = []
    for name, text in sorted(prompts.items()):
        if shared := phrases.shared_with(text):
            out.append(
                f"prompt {name} repeats {len(shared)} phrase(s) of an eval ask, such as {shared[0]!r}. {HOW_TO_FIX}"
            )
    return out


def test_no_prompt_or_tool_description_carries_an_ask() -> None:
    texts = {**prompt_evidence.prompt_texts(), **tool_texts()}
    asked = dataset_asks()
    found = problems(texts, Phrases([ask for ask in asked if ask not in ALLOWED]))
    for ask, (names, _) in ALLOWED.items():
        found += problems({name: text for name, text in texts.items() if name not in names}, Phrases([ask]))
    assert not found, "\n".join(found)


def test_every_allowed_leak_is_still_there() -> None:
    texts = prompt_evidence.prompt_texts()
    asked = set(dataset_asks())
    for ask, (names, _) in ALLOWED.items():
        assert ask in asked, f"no dataset asks {ask!r} any more: remove its ALLOWED entry"
        for name in sorted(names):
            assert Phrases([ask]).shared_with(texts[name]), (
                f"prompt {name} no longer carries {ask!r}: remove it from its ALLOWED entry"
            )


def test_the_law_reads_only_what_is_in_the_repo() -> None:
    assert not [p for p in DATASETS.rglob("*") if "heldout" in p.parts], (
        "the held-out twin must stay outside the repo"
    )
    assert dataset_asks()
    assert tool_texts()


def test_the_law_bites_on_a_planted_tool_description(tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "planted.py").write_text(
        "def lookup(q: str) -> str:\n"
        '    """Search the web. Example: what is the weather in Hyderabad right now today."""\n'
        "TOLD = dict(description='Use it when they say what is the weather in Hyderabad right now')\n"
    )
    phrases = Phrases(["what is the weather in Hyderabad right now?"])
    assert len(problems(tool_texts(tools), phrases)) == 2


def test_the_law_bites_on_a_planted_phrase() -> None:
    phrases = Phrases(
        ["Please turn these quarterly notes into a short board memo", "Hey there my good friend"]
    )
    planted = "Rules.\n\nFor example: turn these quarterly notes into a short board memo, then stop."
    assert problems({"writer": planted}, phrases)
    assert "writer" in problems({"writer": planted}, phrases)[0]
    # A short ask is policed only when it appears whole.
    assert problems({"namer": "Greet with: Hey there my good friend!"}, phrases)
    assert not problems({"namer": "Say hey there to the person."}, phrases)
    # The same rule in other words passes.
    assert not problems({"writer": "Turn rough notes into a memo for a board, kept short."}, phrases)


def test_a_paste_is_not_the_ask() -> None:
    row = OutcomeRow(
        id="x",
        message="Tidy this for Slack\n\nHey team the quarterly billing migration is nearly done",
        expected_outcome=Outcome.REPLY,
    )
    assert asks(row) == ["Tidy this for Slack"]
