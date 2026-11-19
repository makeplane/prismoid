# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Approvals as data: what a person must approve, keyed by the act a call is, never by its words.

A door (the connector hook, the browser) describes the call it is about to make as an `Act`; the run
adds what it knows (has it read a third party's text, did a teammate steer it, is anyone there to
answer) and the result is a `Call`. A `Rule` matches calls by those facts and decides `allow`,
`ask` or `deny`; `ai/rules/approvals.py` evaluates a table of them, deny over ask over allow
(Claude Code's order). A call no rule matches runs: a read, or a change to the task's own Machine,
never reaches a person; an `ask` waits for a person's yes before the call acts.

Every rule carries calls it must match and calls it must miss, checked when the table loads (the
Codex execpolicy practice), so a rule that does not say what it means fails at import, never at run.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from enum import StrEnum
from typing import Any, Final, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from prismoid.schemas.side_effects import SideEffect


class Decision(StrEnum):
    """What a rule says about a call it matches, strongest first when several match."""

    DENY = "deny"
    ASK = "ask"
    ALLOW = "allow"


class Door(StrEnum):
    """Which door a call leaves the run through."""

    CONNECTOR = "connector"  # a tool on a connected service
    BROWSER = "browser"  # the operator's browser


class Act(BaseModel):
    """What one call would do, as its door sees it before running it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    door: Door
    effect: SideEffect
    irreversible: bool = False  # cannot be taken back: a paying or deleting act, a destructive tool
    widens_egress: bool = False  # reaches a host this task has not reached
    owner_asks: bool = False  # the connector's owner set this tool to ask
    moves_money: bool = False  # pays, transfers, refunds or charges (`moves_money`)


class Call(Act):
    """An act together with the run's facts: what a rule matches."""

    tainted: bool = False  # the run has read a third party's text, or a step that fed it did
    borrowed: bool = False  # a teammate's steer entered the run
    attended: bool = True  # a person can answer an ask (False: a routine's run)


class Match(BaseModel):
    """The facts a rule keys on; an unset key matches any call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    door: Door | None = None
    effect: frozenset[SideEffect] | None = None
    irreversible: bool | None = None
    widens_egress: bool | None = None
    owner_asks: bool | None = None
    moves_money: bool | None = None
    tainted: bool | None = None
    borrowed: bool | None = None
    attended: bool | None = None

    @model_validator(mode="after")
    def _names_a_fact(self) -> Self:
        if not self.keys():
            raise ValueError("a rule must key on at least one fact; one that matches every call is a mistake")
        return self

    def keys(self) -> dict[str, object]:
        """The facts this match sets."""
        return {key: want for key, want in self.model_dump().items() if want is not None}

    def matches(self, call: Call) -> bool:
        """Whether every fact this match sets holds for `call`."""
        for key, want in self.keys().items():
            have = getattr(call, key)
            if not (have in want if isinstance(want, frozenset) else have == want):
                return False
        return True


class Rule(BaseModel):
    """One approval rule: the facts it keys on, its decision, and the calls that prove it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    decision: Decision
    when: Match
    why: str = Field(min_length=1)  # the reason, in words a person reads and the model is told on a refusal
    hits: tuple[Call, ...] = Field(min_length=1)  # calls the rule must match
    misses: tuple[Call, ...] = Field(min_length=1)  # calls it must not match

    @model_validator(mode="after")
    def _examples_hold(self) -> Self:
        if wrong := [c for c in self.hits if not self.when.matches(c)]:
            raise ValueError(f"rule {self.id} misses a call it must match: {wrong[0]!r}")
        if wrong := [c for c in self.misses if self.when.matches(c)]:
            raise ValueError(f"rule {self.id} matches a call it must miss: {wrong[0]!r}")
        return self


# What a door is handed to ask: None to run, a refusal the model reads, or it raises the approval.
Approve = Callable[[Act, bool, Mapping[str, Any]], str | None]


def _write(**facts: bool) -> Call:
    return Call(door=Door.CONNECTOR, effect=SideEffect.WORLD, **facts)


def _read(**facts: bool) -> Call:
    return Call(door=Door.CONNECTOR, effect=SideEffect.READ, **facts)


def _click(**facts: bool) -> Call:
    return Call(door=Door.BROWSER, effect=SideEffect.WORLD, **facts)


# The words a server's own tool name uses for moving money. MCP has no payment hint, so the name is
# the one fact a door has; a write whose name carries one asks (`moves-money`), a read never does.
MONEY_WORDS: Final[frozenset[str]] = frozenset(
    {
        "pay", "pays", "payment", "payments", "payout", "payouts", "money", "transfer", "transfers",
        "funds", "wire", "remit", "remittance", "refund", "refunds", "charge", "charges", "purchase",
        "checkout", "withdraw", "withdrawal", "deposit",
    }
)  # fmt: skip
_WORD = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])")


def moves_money(tool: str) -> bool:
    """Whether a tool's own name says it moves money: any snake, kebab or camelCase word in `MONEY_WORDS`."""
    return any(word.lower() in MONEY_WORDS for word in _WORD.findall(tool))


RULES: Final[tuple[Rule, ...]] = (
    Rule(
        id="moves-money",
        decision=Decision.ASK,
        when=Match(effect=frozenset({SideEffect.WORLD}), moves_money=True),
        why="it moves money",
        hits=(_write(moves_money=True), _write(moves_money=True, attended=False)),
        misses=(_write(), _read(moves_money=True)),
    ),
    Rule(
        id="owner-asks",
        decision=Decision.ASK,
        when=Match(owner_asks=True),
        why="its owner set this tool to ask",
        hits=(_write(owner_asks=True), _read(owner_asks=True, attended=False)),
        misses=(_write(),),
    ),
    Rule(
        id="teammate-steered",
        decision=Decision.ASK,
        when=Match(door=Door.CONNECTOR, borrowed=True),
        why="a teammate steered this run",
        hits=(_read(borrowed=True), _write(borrowed=True)),
        misses=(_write(),),
    ),
    Rule(
        id="write-after-outside-text",
        decision=Decision.ASK,
        when=Match(door=Door.CONNECTOR, effect=frozenset({SideEffect.WORLD}), tainted=True, attended=True),
        why="this task read outside text",
        hits=(_write(tainted=True),),
        misses=(_read(tainted=True), _write(tainted=True, attended=False), _click(tainted=True), _write()),
    ),
    Rule(
        id="destructive-tool",
        decision=Decision.ASK,
        when=Match(door=Door.CONNECTOR, irreversible=True, attended=True),
        why="its server marks this tool destructive",
        hits=(_write(irreversible=True),),
        misses=(_write(), _write(irreversible=True, attended=False)),
    ),
    Rule(
        id="irreversible-browser-act",
        decision=Decision.ASK,
        when=Match(door=Door.BROWSER, irreversible=True),
        why="the act cannot be taken back",
        hits=(_click(irreversible=True), _click(irreversible=True, attended=False)),
        misses=(_click(tainted=True),),
    ),
    Rule(
        id="egress-widening",
        decision=Decision.ASK,
        when=Match(widens_egress=True),
        why="it reaches a site this task has not reached",
        hits=(_click(widens_egress=True, attended=False),),
        misses=(_click(),),
    ),
)
