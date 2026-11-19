# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""How a follow-up's dials merge into the task row - the pure half of `tasks.continue_task`.

A follow-up's attachments append, its skill and bot pins supersede by name, its connectors union by
id, and a budget is recorded only when it changes. These rules read nothing and write nothing: the
verb resolves the refs and holds the row lock; `merge_into` writes the row (no I/O, no commit).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from prismoid.db.models.task import Task
from prismoid.errors import PolicyDenied
from prismoid.operations import task_door
from prismoid.schemas.inputs import InputSet
from prismoid.schemas.outputs import ArtifactType
from prismoid.schemas.plan import TaskMode


def refuse_shadowing(taken: set[str], attachments: InputSet) -> None:
    """Refuse a new attachment whose filename an earlier turn already used: a turn-2 `brief.md`
    would silently shadow turn-1's at the same frame path."""
    for item in attachments.items:
        if item.filename in taken:
            raise PolicyDenied(f"{item.filename} already exists on this task; use a new filename")


def superseded_by_name(
    existing: Sequence[dict[str, Any]], fresh: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Pins superseded by name: a later turn naming `reviewer` means THIS version of it from here
    on, and the turns that already ran keep the version they ran. Fresh pins go last."""
    names = {p["name"] for p in fresh}
    return [p for p in existing if p["name"] not in names] + list(fresh)


def union_in_order(existing: Sequence[str], fresh: Sequence[str]) -> list[str]:
    """Ids unioned, order preserved: earlier turns' connectors stay attached."""
    return list(existing) + [c for c in fresh if c not in existing]


def budget_changed(before: Mapping[str, object] | None, after: Mapping[str, object] | None) -> bool:
    """Only a cap that actually changed is recorded: `after` None means the turn
    did not touch it, and `{}` and None stay distinct."""
    return after is not None and after != before


@dataclass(frozen=True, slots=True)
class TurnDials:
    """A follow-up's dials, resolved before the row lock: what merges into the row under it."""

    attachments: InputSet
    skill_pins: list[dict[str, Any]]
    bot_pins: list[dict[str, Any]]
    connector_ids: list[str]
    deliverable: str | None


def merge_into(
    task: Task,
    dials: TurnDials,
    *,
    budget: dict[str, object] | None,
    models: Sequence[str],
    deliverable_format: str | None,
    quality: str | None,
    scale: str | None,
    target_pages: int | None,
) -> None:
    """Merge a follow-up's dials into the locked task row (no commit): attachments append, pins supersede
    by name, connectors union, every other dial set replaces."""
    if dials.attachments.items:
        task.inputs = [*task.inputs, *(i.model_dump(mode="json") for i in dials.attachments.items)]
    if dials.skill_pins:
        task.skill_pins = superseded_by_name(task.skill_pins, dials.skill_pins)
    if dials.bot_pins:
        task.bot_pins = superseded_by_name(task.bot_pins, dials.bot_pins)
    if dials.connector_ids:
        task.connector_ids = union_in_order(task.connector_ids, dials.connector_ids)
    if budget is not None:
        task.budget = budget  # a follow-up may raise the cap; spend stays one ledger
    if models:
        task.model_refs = list(models)
    if dials.deliverable is not None:
        task.deliverable = dials.deliverable
        task.deliverable_source = "user"  # a fresh-turn dial is the user's choice
        if dials.deliverable != ArtifactType.DOCUMENT.value:
            # A kind change clears the format NOW, not at planning: the row is the wire
            # read-back, and serving (deck, pdf) through the clarifying window - or forever,
            # if the turn is cancelled before planning - is a pair the door itself 422s.
            task.deliverable_format = None
    if deliverable_format is not None:
        task.deliverable_format = deliverable_format
    if quality is not None:
        task.quality = quality  # a fresh-turn dial, like budget/models/deliverable
    if scale is not None:
        if TaskMode(task.mode) in (TaskMode.FLEET, TaskMode.COUNCIL):  # council: legacy fleet rows
            task_door.refuse_big_fleet(scale)
        task.scale = scale  # a settled standard thread may continue at any size
    if target_pages is not None:
        task.target_pages = target_pages
