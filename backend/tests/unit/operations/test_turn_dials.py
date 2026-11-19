# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""How a follow-up's dials merge into the task row. No I/O: the row is an unsaved `Task`, the dials
already resolved, the way `tasks.continue_task` hands them over under the row lock."""

from __future__ import annotations

from typing import Any

import pytest

from prismoid.db.models.task import Task
from prismoid.errors import PolicyDenied, ValidationFailed
from prismoid.operations.turn import dials
from prismoid.schemas.inputs import InputSet, TaskInput
from prismoid.schemas.outputs import ArtifactType
from prismoid.schemas.plan import TaskMode


def _input(name: str) -> TaskInput:
    return TaskInput(filename=name, sha256="0" * 64, bytes=1)


def _task(**over: Any) -> Task:
    fields: dict[str, Any] = {
        "mode": TaskMode.STANDARD.value,
        "inputs": [],
        "skill_pins": [],
        "bot_pins": [],
        "connector_ids": [],
        "budget": None,
        "deliverable": None,
        "deliverable_format": None,
    }
    fields.update(over)
    return Task(**fields)


def _dials(**over: Any) -> dials.TurnDials:
    fields: dict[str, Any] = {
        "attachments": InputSet(items=[]),
        "skill_pins": [],
        "bot_pins": [],
        "connector_ids": [],
        "deliverable": None,
    }
    fields.update(over)
    return dials.TurnDials(**fields)


def _merge(task: Task, given: dials.TurnDials, **over: Any) -> None:
    kw: dict[str, Any] = {
        "budget": None,
        "models": (),
        "deliverable_format": None,
        "quality": None,
        "scale": None,
        "target_pages": None,
    }
    kw.update(over)
    dials.merge_into(task, given, **kw)


def test_a_new_filename_may_not_shadow_an_earlier_turns() -> None:
    dials.refuse_shadowing({"brief.md"}, InputSet(items=[_input("notes.md")]))
    with pytest.raises(PolicyDenied, match=r"brief\.md already exists"):
        dials.refuse_shadowing({"brief.md"}, InputSet(items=[_input("brief.md")]))


def test_pins_supersede_by_name_and_go_last() -> None:
    old = [{"name": "reviewer", "version": 1}, {"name": "report", "version": 2}]
    fresh = [{"name": "reviewer", "version": 3}]
    assert dials.superseded_by_name(old, fresh) == [
        {"name": "report", "version": 2},
        {"name": "reviewer", "version": 3},
    ]


def test_connectors_union_in_order() -> None:
    assert dials.union_in_order(["a", "b"], ["b", "c"]) == ["a", "b", "c"]


def test_only_a_cap_that_changed_is_recorded() -> None:
    assert not dials.budget_changed({"usd": 2}, None), "an untouched cap"
    assert not dials.budget_changed({"usd": 2}, {"usd": 2})
    assert dials.budget_changed({"usd": 2}, {"usd": 5})
    assert dials.budget_changed(None, {}), "{} and None stay distinct"


def test_a_merge_appends_inputs_and_merges_pins_and_connectors() -> None:
    task = _task(
        inputs=[_input("a.md").model_dump(mode="json")],
        skill_pins=[{"name": "report", "version": 1}],
        connector_ids=["c1"],
    )
    _merge(
        task,
        _dials(
            attachments=InputSet(items=[_input("b.md")]),
            skill_pins=[{"name": "report", "version": 2}],
            bot_pins=[{"name": "helper", "version": 1}],
            connector_ids=["c1", "c2"],
        ),
    )
    assert [i["filename"] for i in task.inputs] == ["a.md", "b.md"]
    assert task.skill_pins == [{"name": "report", "version": 2}]
    assert task.bot_pins == [{"name": "helper", "version": 1}]
    assert task.connector_ids == ["c1", "c2"]


def test_a_kind_change_clears_the_format_unless_the_turn_sets_one() -> None:
    task = _task(deliverable=ArtifactType.DOCUMENT.value, deliverable_format="pdf")
    _merge(task, _dials(deliverable=ArtifactType.DECK.value))
    assert (task.deliverable, task.deliverable_source, task.deliverable_format) == ("deck", "user", None)
    task = _task(deliverable=ArtifactType.DECK.value)
    _merge(task, _dials(deliverable=ArtifactType.DOCUMENT.value), deliverable_format="docx")
    assert (task.deliverable, task.deliverable_format) == ("document", "docx")


def test_the_other_dials_replace_only_when_set() -> None:
    task = _task(budget={"usd": 1}, quality="standard", scale="brief", target_pages=3)
    _merge(task, _dials())
    assert (task.budget, task.quality, task.scale, task.target_pages) == ({"usd": 1}, "standard", "brief", 3)
    _merge(task, _dials(), budget={"usd": 4}, models=["m"], quality="max", scale="long", target_pages=40)
    assert (task.budget, task.model_refs, task.quality, task.scale, task.target_pages) == (
        {"usd": 4},
        ["m"],
        "max",
        "long",
        40,
    )


def test_a_fleet_thread_refuses_a_big_size() -> None:
    with pytest.raises(ValidationFailed):
        _merge(_task(mode=TaskMode.FLEET.value), _dials(), scale="long")
