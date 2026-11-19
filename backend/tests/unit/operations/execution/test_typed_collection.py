# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The typed collection path (ADR-019 speed pass), pinned: the organizer's OUTPUT is the
schema-valid collection, and the SYSTEM walks it through the one door."""

from __future__ import annotations

import uuid
from typing import Any

import orjson

from prismoid.operations.execution.publish import publish_collection_result as _publish_collection_result
from prismoid.schemas.collection import Collection
from prismoid.schemas.ids import StepId, TaskId
from prismoid.schemas.outputs import CollectionResult, get


def _a_result() -> CollectionResult:
    return CollectionResult(
        filename="launch-tracker",
        summary="Three workstreams on a board.",
        collection=Collection.model_validate(
            {
                "collection_version": 1,
                "title": "Launch tracker",
                "fields": [
                    {"key": "name", "label": "Name", "type": "text"},
                    {"key": "stage", "label": "Stage", "type": "select", "options": ["planned"]},
                ],
                "rows": [{"id": "r1", "cells": {"name": "API", "stage": "planned"}}],
                "views": [{"name": "Board", "layout": "board", "group_by": "stage"}],
            }
        ),
    )


class _FakeFrame:
    def __init__(self) -> None:
        self.writes: list[tuple[str, bytes]] = []
        self.published: list[tuple[str, str, str, list[str]]] = []

    async def write_bytes(self, path: str, data: bytes, **_: Any) -> None:
        self.writes.append((path, data))

    async def publish(self, path: str, type: str, title: str, sources: list[str] | None = None) -> None:
        self.published.append((path, type, title, list(sources or [])))


class _Deps:
    def __init__(self) -> None:
        self.task_id = TaskId(uuid.uuid4())
        self.step_id = StepId(uuid.uuid4())
        self.inputs = ("/task/inputs/brief.md",)


async def test_the_system_publishes_the_typed_collection_through_the_one_door() -> None:
    frame = _FakeFrame()
    out = _a_result()
    facts: list[str] = []

    async def fact(phase: str, label: str, *, started: float | None = None) -> None:
        assert frame.published, "the engine fact lands AFTER the door - never before the act"
        facts.append(label)

    await _publish_collection_result(frame, _Deps(), out, fact=fact)

    assert facts == ["Published launch-tracker.collection.json · collection"]
    [(path, data)] = frame.writes
    assert path == "/work/launch-tracker.collection.json"
    assert Collection.model_validate(orjson.loads(data)).title == "Launch tracker", (
        "the stored bytes are the canonical, already-valid collection"
    )
    [(pub_path, pub_type, pub_title, pub_sources)] = frame.published
    assert pub_path == path and pub_type == "collection" and pub_title == "Launch tracker"
    assert pub_sources == ["/task/inputs/brief.md"], "sources stamped by the system, like the tool"


class _OnTheirComputer(_FakeFrame):
    staging_dir = "/out"


async def test_on_the_persons_computer_a_collection_is_not_written_into_their_folder() -> None:
    frame = _OnTheirComputer()

    async def fact(phase: str, label: str, *, started: float | None = None) -> None:
        return None

    await _publish_collection_result(frame, _Deps(), _a_result(), fact=fact)
    [(path, _)] = frame.writes
    assert path == "/out/launch-tracker.collection.json"


def test_the_output_registry_serves_collection_result() -> None:
    assert get("collection_result") is CollectionResult
    assert _a_result().summary, "narration_of reads .summary - the thread gets the model's words"


def test_the_depth_clause_rides_every_collection_contract() -> None:
    """A collection's acceptance asks for substance in every row; a row band rides it only when the
    person sized the ask, and then their own number. Kinds whose depth the ask fixes carry no clause."""
    from prismoid.ai.rules.plan import depth_clause
    from prismoid.operations.plan_shapes import one_step_plan
    from prismoid.schemas.outputs import ArtifactType
    from prismoid.schemas.sizing import SizeBucket

    assert depth_clause(ArtifactType.DOCUMENT) == []
    clause = depth_clause(ArtifactType.COLLECTION)
    assert any("rows carry substance" in line for line in clause)
    assert not any("rows is the band" in line or "ceiling" in line for line in clause), "no number, no band"

    [(_, spec)], _, _ = one_step_plan("track the migration", agent="maker", produces=ArtifactType.COLLECTION)
    assert spec.contract is not None
    assert any("rows carry substance" in a for a in spec.contract.acceptance)
    assert not any("ceiling" in a for a in spec.contract.acceptance)

    [(_, sized)], _, _ = one_step_plan(
        "track 40 migrations",
        agent="maker",
        produces=ArtifactType.COLLECTION,
        bucket=SizeBucket.EXTENDED,
        units=40,
    )
    assert sized.contract is not None
    assert any("~40 rows" in a for a in sized.contract.acceptance), "the dialed count is the band"
    [(_, doc_spec)], _, _ = one_step_plan("write the memo")
    assert doc_spec.contract is not None
    assert not any("rows" in a for a in doc_spec.contract.acceptance)
