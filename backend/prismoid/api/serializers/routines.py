# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Routines - request bodies and responses. `*In` = request, `*Out` = response.

The schedule crosses the wire as the user wrote it (cron | every | at + timezone) and the
response ECHOES the resolved next instants (`next_fire_at`, `upcoming`) - the creation
confirmation that closes the misparsed-schedule complaint class, and the
Trigger.dev `upcoming` affordance for the detail page.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from prismoid.conf import settings
from prismoid.db.models.routine import Routine, RoutineRun
from prismoid.schemas.routines import (
    INSTRUCTION_MAX,
    FireResult,
    RoutineStatus,
    ScheduleSpec,
    TriggerKind,
    jitter_offset_s,
    parse_duration_s,
    upcoming_after,
)


class ScheduleIn(BaseModel):
    """Exactly one of cron / every / at (the one-of law lives in schemas - operations turn its
    sentence into the 422). `every` takes the duration grammar ("30m", "4h") and is checked at
    this door so a typo is a 422 naming the field, never a 500; `every_s` is the resolved wire
    form and either spelling is accepted. `timezone: None` ⇒ the owner's profile timezone
    (operations resolves it - the serializer has no session)."""

    model_config = ConfigDict(frozen=True)

    cron: str | None = Field(default=None, max_length=120)
    every: str | None = Field(default=None, max_length=16)
    every_s: int | None = None
    at: dt.datetime | None = None
    timezone: str | None = Field(default=None, max_length=64)

    @field_validator("every")
    @classmethod
    def _duration_grammar(cls, v: str | None) -> str | None:
        if v is not None:
            parse_duration_s(v)  # raises ValueError → FastAPI's 422, on this field
        return v

    def to_spec_dict(self) -> dict[str, object]:
        every_s = (
            self.every_s
            if self.every_s is not None
            else (parse_duration_s(self.every) if self.every else None)
        )
        return {"cron": self.cron, "every_s": every_s, "at": self.at, "timezone": self.timezone}


class RoutineCreateIn(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1, max_length=120)
    # INSTRUCTION_MAX, not the column's 8000: headroom under `tasks.goal` for the payload
    # fence (schemas.routines).
    instruction: str = Field(min_length=1, max_length=INSTRUCTION_MAX)
    # exactly one When: a schedule, XOR the events to listen for (ops enforces the XOR)
    schedule: ScheduleIn | None = None
    on_events: list[str] | None = Field(default=None, max_length=5)
    event_verdict: Literal["pass", "fail"] | None = None
    project_id: UUID | None = None
    budget_usd: float | None = Field(default=None, gt=0, le=1000)
    bot: str | None = Field(default=None, max_length=64)  # the keeper - the direct lane
    skills: list[str] = Field(default_factory=list, max_length=8)
    connectors: list[str] = Field(default_factory=list, max_length=8)
    deliverable: str | None = None
    deliverable_format: str | None = None
    quality: str | None = None
    # Behaviours. Literals ARE the validation (the quality-dial precedent).
    overlap: Literal["skip", "queue", "replace"] | None = None
    catch_up: Literal["skip", "once"] | None = None
    notify: Literal["always", "findings", "failure", "never"] | None = None
    posture: Literal["auto", "hold"] | None = None
    monthly_cap_usd: float | None = Field(default=None, gt=0, le=10000)
    until_at: dt.datetime | None = None
    max_runs: int | None = Field(default=None, ge=1, le=100000)
    # The size dial - part of "the SAME payload, saved instead of sent".
    scale: Literal["auto", "brief", "standard", "extended", "long", "book"] | None = None

    @model_validator(mode="after")
    def _verdict_needs_events(self) -> RoutineCreateIn:
        if self.event_verdict is not None and self.on_events is None:
            raise ValueError("event_verdict narrows verification.signed - it needs on_events")
        return self

    def ops_kwargs(self) -> dict[str, Any]:
        """The ops call's kwargs, spelled once - the view stays auth → validate → one call →
        serialize (field names match `ops.create_routine`'s signature by construction)."""
        kwargs = self.model_dump()
        kwargs["schedule"] = self.schedule.to_spec_dict() if self.schedule is not None else None
        return kwargs


class RoutineUpdateIn(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str | None = Field(default=None, min_length=1, max_length=120)
    instruction: str | None = Field(default=None, min_length=1, max_length=INSTRUCTION_MAX)
    schedule: ScheduleIn | None = None
    budget_usd: float | None = Field(default=None, gt=0, le=1000)
    overlap: Literal["skip", "queue", "replace"] | None = None
    catch_up: Literal["skip", "once"] | None = None
    notify: Literal["always", "findings", "failure", "never"] | None = None
    posture: Literal["auto", "hold"] | None = None
    monthly_cap_usd: float | None = Field(default=None, gt=0, le=10000)
    until_at: dt.datetime | None = None
    max_runs: int | None = Field(default=None, ge=1, le=100000)

    def ops_kwargs(self) -> dict[str, Any]:
        kwargs = self.model_dump()
        kwargs["schedule"] = self.schedule.to_spec_dict() if self.schedule is not None else None
        return kwargs


class PauseIn(BaseModel):
    model_config = ConfigDict(frozen=True)

    note: str | None = Field(default=None, max_length=400)


class RoutineOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    name: str
    instruction: str
    status: str  # active | paused | retired
    paused_reason: str | None
    pause_note: str | None
    trigger_kind: str  # schedule | event (api-kind is the door every routine already has)
    schedule: dict[str, object] | None  # the spec as stored: cron/every_s/at + timezone
    events: dict[str, object] | None  # the event spec as stored: kinds + verdict
    project_id: UUID | None
    owner_id: UUID  # the owner's USER id - the API presents owners as users, never actors
    keeper: str  # "one", or the bot's name - who answers every run
    budget_usd_per_run: float
    overlap: str
    catch_up: str
    notify: str
    posture: str
    next_fire_at: dt.datetime | None
    last_fired_at: dt.datetime | None
    upcoming: list[dt.datetime]  # the next few resolved instants - the confirmation echo
    monthly_cap_usd: float | None
    # month-to-date spend (the ledger join) - composed on the SINGLE get only; list rows carry
    # None rather than an N+1 of aggregates (the count law's cousin: no faked numbers).
    spend_mtd_usd: float | None
    consecutive_failures: int
    until_at: dt.datetime | None
    max_runs: int | None
    runs_minted: int
    created_at: dt.datetime


class RoutineRunOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    occurrence: str
    source: str  # clock | api | event | manual
    decision: str  # minted | held | skipped_* | superseded | error - every non-fire says why
    task_id: UUID | None
    task_status: str | None  # the outcome lives on the task (infra-green ≠ task-success)
    scheduled_for: dt.datetime | None
    at: dt.datetime
    detail: str


class FireTokenOut(BaseModel):
    """Shown ONCE (the ck_ ceremony). Rotation supersedes; revocation closes the door."""

    model_config = ConfigDict(frozen=True)

    token: str


class FireOut(BaseModel):
    """A 202 whatever was decided: a refused fire is a RESULT wearing its journaled reason."""

    model_config = ConfigDict(frozen=True)

    run_id: UUID | None
    decision: str
    task_id: UUID | None
    detail: str


def routine_out(row: Routine, *, spend_mtd: float | None = None, with_upcoming: bool = True) -> RoutineOut:
    template = row.template or {}
    schedule = row.trigger if row.trigger_kind == TriggerKind.SCHEDULE else None
    # `upcoming[0]` IS the stored next_fire_at, successors walk the same jittered lane the gate
    # advances on - preview(after=now) re-anchored mid-phase and dropped the jitter, so one
    # response carried two different "next" instants. Lists skip it: the
    # rows render next_fire_at only, and n×3 cron walks per page were pure waste (the spend_mtd
    # exemption's cousin).
    upcoming: list[dt.datetime] = []
    if (
        with_upcoming
        and schedule is not None
        and row.status == RoutineStatus.ACTIVE
        and row.next_fire_at is not None
    ):
        upcoming = upcoming_after(
            ScheduleSpec.model_validate(schedule),
            first=row.next_fire_at,
            n=3,
            jitter_s=jitter_offset_s(row.id, settings.routines.jitter_max_s),
        )
    return RoutineOut(
        id=row.id,
        name=row.name,
        instruction=row.instruction,
        status=row.status,
        paused_reason=row.paused_reason,
        pause_note=row.pause_note,
        trigger_kind=row.trigger_kind,
        schedule=schedule,
        events=row.trigger if row.trigger_kind == TriggerKind.EVENT else None,
        project_id=row.project_id,
        owner_id=row.owner_actor_id,  # User rides the actor's id (the satellite key) - same
        # UUID, the USER noun (projects.py's owner precedent)
        keeper=str(template.get("bot") or "one"),
        budget_usd_per_run=float(row.budget_usd_per_run),
        overlap=row.overlap,
        catch_up=row.catch_up,
        notify=row.notify,
        posture=row.posture,
        next_fire_at=row.next_fire_at,
        last_fired_at=row.last_fired_at,
        upcoming=upcoming,
        monthly_cap_usd=float(row.monthly_cap_usd) if row.monthly_cap_usd is not None else None,
        spend_mtd_usd=spend_mtd,
        consecutive_failures=row.consecutive_failures,
        until_at=row.until_at,
        max_runs=row.max_runs,
        runs_minted=row.runs_minted,
        created_at=row.created_at,
    )


def run_out(row: RoutineRun, task_status: str | None) -> RoutineRunOut:
    return RoutineRunOut(
        id=row.id,
        occurrence=row.occurrence,
        source=row.source,
        decision=row.decision,
        task_id=row.task_id,
        task_status=task_status,
        scheduled_for=row.scheduled_for,
        at=row.created_at,
        detail=row.detail,
    )


def fire_out(result: FireResult) -> FireOut:
    return FireOut(
        run_id=result.run_id,
        decision=result.decision.value,
        task_id=result.task_id,
        detail=result.detail,
    )
