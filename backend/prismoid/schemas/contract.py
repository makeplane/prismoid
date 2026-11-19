# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The contract - what a step is asked for, and how we will know it delivered.

This is the value the Planner writes and the executor reads. It is deliberately *checkable*:
`acceptance` is a list of criteria a verifier can test, not a mood. The operator's voice puts it as
*write goals like acceptance criteria, not vibes*, and this schema is where that stops being
advice and starts being a type.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from prismoid.schemas.ids import BotName, SkillName
from prismoid.schemas.outputs import OutputRef
from prismoid.schemas.usage import Budget


class SkillPin(BaseModel):
    """A skill resolved to a concrete version at plan time. Reproducibility is the point:
    publishing a new version never changes what a running task is doing."""

    model_config = ConfigDict(frozen=True)

    name: SkillName
    version: int | None = None  # None ⇒ the author's draft (`report@draft`, author-only)

    def ref(self) -> str:
        return f"{self.name}@{'draft' if self.version is None else self.version}"


class BotPin(BaseModel):
    """A bot resolved to a concrete version at plan time - `SkillPin`'s twin, and for the same
    reason: publishing v2 of a bot must never change what a running task is doing.

    This is the temporal half of composability, and it is why bots can be edited fearlessly."""

    model_config = ConfigDict(frozen=True)

    name: BotName
    version: int | None = None  # None ⇒ the author's draft (`reviewer@draft`, author-only)

    def ref(self) -> str:
        return f"{self.name}@{'draft' if self.version is None else self.version}"


class Contract(BaseModel):
    """What one step owes the task."""

    model_config = ConfigDict(frozen=True)

    goal: str = Field(min_length=1)
    acceptance: list[str] = Field(default_factory=list)  # each one a verifier can check
    inputs: list[str] = Field(default_factory=list)  # frame paths: /task/inputs/…, /work/…
    output: OutputRef | None = None  # None ⇒ the step produces no artifact (research, triage)

    # The egress allowlist for this step, enforced by the proxy - not by asking the agent nicely.
    allow_domains: list[str] = Field(default_factory=list)

    budget: Budget | None = None
    notes: str = ""  # answers from a gate, a merged steer - context, never instructions to cheat


class Criteria(BaseModel):
    """The task's acceptance criteria, as the Planner understood them. Amended by steers;
    the amendment history is kept, because 'what did it think I asked for' is the first question
    anyone asks of a wrong answer."""

    model_config = ConfigDict(frozen=True)

    must: list[str] = Field(default_factory=list)
    should: list[str] = Field(default_factory=list)
    tone: str = ""
    audience: str = ""
    amendments: list[str] = Field(default_factory=list)  # appended by steers, never rewritten
