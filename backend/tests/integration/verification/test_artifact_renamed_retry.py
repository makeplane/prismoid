# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A retry that publishes under a new name revises the draft it replaces: never two live deliverables."""

from __future__ import annotations

import pytest

from prismoid import storage
from prismoid.db.models.artifact import Artifact
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope
from prismoid.operations import artifacts as artifact_ops
from prismoid.schemas.artifacts import Manifest
from tests.support.rigs.machine_execution_rig import task_with_steps

pytestmark = pytest.mark.integration


def _deck(path: str, body: bytes) -> Manifest:
    return Manifest.model_validate(
        {
            "entries": [
                {
                    "path": path,
                    "type": "document",
                    "title": "Pitch deck",
                    "sha256": storage.sha256(body),
                    "bytes": len(body),
                    "sources": [],
                    "agent": "",
                    "model": "",
                    "skill_pins": [],
                    "at": "2026-08-06T01:00:00Z",
                }
            ]
        }
    )


async def test_a_renamed_retry_revises_the_draft() -> None:
    task_id, [step_id] = await task_with_steps(1)
    first, second = b"attempt one", b"attempt two, fixed"
    async with row_scope(Task, task_id) as session:
        [_draft] = await artifact_ops.harvest(
            session, task_id, step_id, _deck("/artifacts/deck.md", first), {"/artifacts/deck.md": first}
        )
        [fixed] = await artifact_ops.harvest(
            session,
            task_id,
            step_id,
            _deck("/artifacts/deck-v2.md", second),
            {"/artifacts/deck-v2.md": second},
        )
        await session.commit()
        shelf = await Artifact.latest_for_task(session, task_id)
        live = [a for a in shelf if a.role == "deliverable"]
        assert [a.id for a in live] == [fixed.id], (
            f"two live deliverables: {[(a.provenance.get('path'), a.revision) for a in live]}"
        )
        assert fixed.revision == 2 and fixed.supersedes_id == _draft.id


async def test_a_retry_retires_the_drafts_it_did_not_republish() -> None:
    task_id, [step_id] = await task_with_steps(1)
    a, b, c = b"deck one", b"appendix one", b"deck two"
    two = Manifest(
        entries=[*_deck("/artifacts/deck.md", a).entries, *_deck("/artifacts/appendix.md", b).entries]
    )
    async with row_scope(Task, task_id) as session:
        await artifact_ops.harvest(
            session, task_id, step_id, two, {"/artifacts/deck.md": a, "/artifacts/appendix.md": b}
        )
        kept = await artifact_ops.harvest(
            session, task_id, step_id, _deck("/artifacts/final.md", c), {"/artifacts/final.md": c}
        )
        retired = await artifact_ops.retire_drafts(session, step_id, kept)
        await session.commit()
        assert len(retired) == 2
        shelf = [x for x in await Artifact.latest_for_task(session, task_id) if x.role == "deliverable"]
        assert [x.id for x in shelf] == [kept[0].id]


async def test_a_retry_keeps_the_revision_of_an_earlier_turns_file() -> None:
    task_id, [earlier, step_id] = await task_with_steps(2)
    a, b, c, d = b"report turn one", b"report retried", b"summary", b"data"
    async with row_scope(Task, task_id) as session:
        [delivered] = await artifact_ops.harvest(
            session, task_id, earlier, _deck("/artifacts/report.md", a), {"/artifacts/report.md": a}
        )
        [revised] = await artifact_ops.harvest(
            session, task_id, step_id, _deck("/artifacts/report.md", b), {"/artifacts/report.md": b}
        )
        revised.lineage_id, revised.revision = delivered.lineage_id, 2  # as a later turn's revision is
        two = Manifest(
            entries=[*_deck("/artifacts/summary.md", c).entries, *_deck("/artifacts/data.md", d).entries]
        )
        kept = await artifact_ops.harvest(
            session, task_id, step_id, two, {"/artifacts/summary.md": c, "/artifacts/data.md": d}
        )
        assert await artifact_ops.retire_drafts(session, step_id, kept) == []
        await session.commit()
        shelf = {x.id for x in await Artifact.latest_for_task(session, task_id) if x.role == "deliverable"}
        assert revised.id in shelf, "the earlier turn's report left the shelf"
