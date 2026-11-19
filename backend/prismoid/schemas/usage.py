# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The money vocabulary. Native units in, `cost_usd` priced at write time, never re-derived.

Every provider call emits one `UsageRecord`. The ledger is append-only and
reconciles to the cent - which is why money is `Decimal` here and `NUMERIC` in Postgres.
A float would lose the cent on the way to the invoice, and "reconciles to about the cent" is
not a thing anyone will accept.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Mapping
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from prismoid.schemas.ids import ConversationId, ProjectId, StepId, TaskId
from prismoid.schemas.model_class import Family


class Unit(StrEnum):
    """Native units. Providers bill in these; we never invent a synthetic 'token'."""

    INPUT_TOKEN = "input_token"
    OUTPUT_TOKEN = "output_token"
    CACHE_READ_TOKEN = "cache_read_token"
    CACHE_WRITE_TOKEN = "cache_write_token"
    CACHE_WRITE_1H_TOKEN = "cache_write_1h_token"  # a 1-hour cache write: Anthropic bills it at 2x input
    FLEX_INPUT_TOKEN = "flex_input_token"  # the flex tier's own rates (OpenAI, Gemini): about half
    FLEX_OUTPUT_TOKEN = "flex_output_token"
    FLEX_CACHE_READ_TOKEN = "flex_cache_read_token"
    REASONING_TOKEN = "reasoning_token"
    REQUEST = "request"
    IMAGE = "image"
    SECOND = "second"  # audio/video providers bill by wall time (the ElevenLabs-shaped hole)
    CHARACTER = "character"
    SEARCH = "search"  # web-search engines bill per query; flat-priced from conf


class UsageRecord(BaseModel):
    """One provider call's bill. The value that becomes one `usage_events` row."""

    model_config = ConfigDict(frozen=True)

    task_id: TaskId | None = None
    step_id: StepId | None = None
    project_id: ProjectId | None = None  # attributed at time of spend; a task move never rewrites it
    conversation_id: ConversationId | None = None  # a chat reply's spend: no task, no step

    provider: str
    model: str
    family: Family
    units: dict[Unit, int]

    cost_usd: Decimal
    price_version: int  # which PriceBook priced it - repricing affects new rows only
    at: dt.datetime


class Budget(BaseModel):
    """A ceiling. `None` means unbounded - which is a decision, not an omission."""

    model_config = ConfigDict(frozen=True)

    usd: Decimal | None = None
    tokens: int | None = None

    def is_unbounded(self) -> bool:
        return self.usd is None and self.tokens is None


# A stored budget's `source` when the deployment set the cap (ADR-087 section 1): nobody chose it,
# so it is never a person's consent to a cost.
DEFAULT_CAP_SOURCE = "default"


def cap_is_default(budget: Mapping[str, Any] | None) -> bool:
    """The stored cap is the deployment's default, not one a person chose."""
    return budget is not None and budget.get("source") == DEFAULT_CAP_SOURCE


def usd_ceiling(budget: Mapping[str, Any] | None) -> Decimal | None:
    """A stored task budget's USD ceiling, or None when it has none (missing, or `usd` None or 0).
    The one reading: the Bursar's `budget_of` and the default cap both ask it."""
    if not budget or budget.get("usd") in (None, 0):
        return None
    return Decimal(str(budget["usd"]))


class PersonSpend(BaseModel):
    """One person's share of a task's spend."""

    model_config = ConfigDict(frozen=True)

    actor_id: uuid.UUID
    name: str
    usd: Decimal


class CostSummary(BaseModel):
    """What a task (or project) has spent. Read from the rollup, never by scanning the ledger."""

    model_config = ConfigDict(frozen=True)

    total_usd: Decimal = Decimal("0")
    by_family: dict[Family, Decimal] = Field(default_factory=dict[Family, Decimal])
    by_step: dict[StepId, Decimal] = Field(default_factory=dict[StepId, Decimal])
    units: dict[Unit, int] = Field(default_factory=dict[Unit, int])
    calls: int = 0
    by_person: tuple[PersonSpend, ...] = ()


class ProjectUsage(BaseModel):
    """A project's month-to-date position against its budget."""

    model_config = ConfigDict(frozen=True)

    project_id: ProjectId
    month: str  # YYYY-MM
    spent_usd: Decimal
    budget_usd: Decimal | None
    by_family: dict[Family, Decimal] = Field(default_factory=dict[Family, Decimal])

    @property
    def remaining_usd(self) -> Decimal | None:
        return None if self.budget_usd is None else self.budget_usd - self.spent_usd
