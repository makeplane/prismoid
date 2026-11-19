# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The thread transcript - a task's conversation, as a value (values live here).

**Never persisted.** The transcript is a deterministic pure function of (the append-only durable
events up to a boundary seq, a token budget): any two builds of the same inputs are identical,
so it is re-derived per use - the planner's brief, the mounted `/task/thread.md`, a fork child's
inheritance - and can never drift from the record it is derived from. The `context.assembled`
event records its token *spend* (the thread lane); the text itself is re-derivable forever.

Construction is append-only in event order and the renderers never re-sort - so under budget,
turn K's render is a byte-prefix of turn K+1's, and the provider prompt cache keeps hitting
across a conversation. Once elision starts, the elision line changes per turn and the cache
breaks past the anchor; honesty outranks the cache.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from prismoid.schemas.ids import ActorId, TaskId

type EntryKind = Literal["user", "assistant", "steering", "qa", "event"]


class AuthorRef(BaseModel):
    """Who said a turn - a value copied from the event's actor, never a row."""

    model_config = ConfigDict(frozen=True)

    id: ActorId
    name: str


class ThreadEntry(BaseModel):
    """One line of the conversation, with its provenance.

    `user` - an ask (the opening goal, a follow-up message) · `assistant` - the computer's own
    prose (a step narration, a non-redundant delivered summary) · `steering` - a mid-flight
    directive · `qa` - an answered question, prompt and answer pre-joined · `event` - an honest
    marker (published, delivered, failed, forked), pre-bracketed `[…]`.
    """

    model_config = ConfigDict(frozen=True)

    kind: EntryKind
    text: str
    task_id: TaskId  # which task in the chain said it (forks walk their parents)
    turn: int  # that task's own turn counter, 1-based; 0 = the conversation before it
    seq: int  # the source event's seq - the line's address in the record
    tokens: int  # estimate at build time; what the budget arithmetic used
    author: AuthorRef | None = None  # the human who said it, when the event named one


class ThreadTurn(BaseModel):
    model_config = ConfigDict(frozen=True)

    task_id: TaskId
    turn: int
    entries: list[ThreadEntry] = Field(default_factory=list[ThreadEntry])


class ThreadTranscript(BaseModel):
    model_config = ConfigDict(frozen=True)

    turns: list[ThreadTurn] = Field(
        default_factory=list[ThreadTurn]
    )  # chain order, seq order within - never re-sorted
    elided_turns: int = 0  # turns the budget dropped (in part or whole); rendered as one honest line
    truncated: bool = False  # any elision or per-entry clip happened
    total_tokens: int = 0
    budget_tokens: int | None = None  # the ceiling it was fitted to (the context meter's denominator)
    current_ask: str | None = None  # the newest user entry - what THIS turn is asking for
    current_author: str | None = None  # the HUMAN who asked it (a user actor's name); never a key or agent

    def is_empty(self) -> bool:
        """Nothing beyond the current turn's own opening ask - a fresh turn-1 task. The brief
        already headlines the goal, so an "empty" transcript renders nowhere."""
        entries = [e for t in self.turns for e in t.entries]
        return len(entries) <= 1 and self.elided_turns == 0
