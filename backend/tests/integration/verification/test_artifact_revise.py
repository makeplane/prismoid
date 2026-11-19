# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Edits are revisions (ADR-015) - the second door, exercised end to end.

The properties under test: a save mints an immutable new row on the same lineage (the base keeps
its bytes and verification); the base sha is real optimistic concurrency (stale ⇒ 409 naming the
newer sha); nothing schema-invalid can enter a lineage (the SAME pydantic model the verifier
mirrors); display paths show lineage-latest while GC still walks every revision; and authz
follows the delete/rename norms (creator or project editor - a stranger sees nothing).

Needs the stack (`just up`).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import orjson
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid import storage
from prismoid.db.models.actor import Actor
from prismoid.db.models.artifact import Artifact
from prismoid.db.session import session_scope
from prismoid.errors import Conflict, NotFound, ValidationFailed
from prismoid.operations import artifacts as ops
from prismoid.operations import tasks as task_ops
from prismoid.schemas.artifacts import Manifest
from tests.support.artifacts import CONTENT, a_collection_artifact
from tests.support.cast import member

pytestmark = pytest.mark.integration


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    async with session_scope() as session:
        yield session


@pytest.fixture
async def actor(db: AsyncSession) -> Actor:
    row, _ = await member(db, "Ana")
    await db.commit()
    return row


async def test_a_save_mints_the_next_revision_with_user_provenance(db: AsyncSession, actor: Actor) -> None:
    base = await a_collection_artifact(db, actor)
    assert (base.lineage_id, base.revision) == (base.id, 1)

    edited: dict[str, Any] = {
        **CONTENT,
        "rows": [{"id": "r1", "cells": {"name": "Line expansion", "stage": "Done"}}],
    }
    revised = await ops.revise(db, actor, base.id, content=edited, base_sha256=base.sha256)

    assert revised.lineage_id == base.lineage_id
    assert revised.revision == 2
    assert revised.supersedes_id == base.id
    assert revised.provenance["agent"] == "user"
    assert revised.provenance["skill_pins"] == []
    # The base row is untouched - append-only extends to people.
    assert base.sha256 != revised.sha256

    latest = await Artifact.latest_for_task(db, base.task_id)
    assert [a.id for a in latest] == [revised.id], "display paths show the lineage's face only"
    everything = await Artifact.for_task(db, base.task_id)
    assert {a.id for a in everything} == {base.id, revised.id}, "GC still walks every revision"


async def test_a_stale_base_is_a_409_that_names_the_newer_sha(db: AsyncSession, actor: Actor) -> None:
    base = await a_collection_artifact(db, actor)
    revised = await ops.revise(db, actor, base.id, content=CONTENT | {"title": "v2"}, base_sha256=base.sha256)

    with pytest.raises(Conflict) as clash:
        await ops.revise(db, actor, base.id, content=CONTENT | {"title": "v3?"}, base_sha256=base.sha256)
    assert revised.sha256 in str(clash.value), "the 409 must hand the client its rebase target"


async def test_nothing_schema_invalid_enters_a_lineage(db: AsyncSession, actor: Actor) -> None:
    base = await a_collection_artifact(db, actor)
    broken: dict[str, Any] = {**CONTENT, "rows": [{"id": "r1", "cells": {"stage": "Shipped"}}]}
    with pytest.raises(ValidationFailed):
        await ops.revise(db, actor, base.id, content=broken, base_sha256=base.sha256)


async def test_a_stranger_cannot_even_see_the_artifact(db: AsyncSession, actor: Actor) -> None:
    base = await a_collection_artifact(db, actor)
    stranger, _ = await member(db, "Bo")
    await db.commit()
    with pytest.raises(NotFound):  # scope precedes authz - existence is privileged
        await ops.revise(db, stranger, base.id, content=CONTENT, base_sha256=base.sha256)


async def test_the_revised_event_lands_on_the_record(db: AsyncSession, actor: Actor) -> None:
    from prismoid.db.models.event import TaskEvent

    base = await a_collection_artifact(db, actor)
    revised = await ops.revise(db, actor, base.id, content=CONTENT | {"title": "v2"}, base_sha256=base.sha256)

    from prismoid.schemas.events import ArtifactRevised

    events = await TaskEvent.replay(db, base.task_id)
    revised_events = [e for e in events if isinstance(e, ArtifactRevised)]
    assert len(revised_events) == 1
    event = revised_events[0]
    assert event.artifact_id == revised.id
    assert event.supersedes_id == base.id
    assert event.revision == 2


async def a_document_artifact(
    db: AsyncSession, actor: Actor, *, path: str = "/artifacts/notes.md", preview: bytes | None = None
) -> Artifact:
    from prismoid.operations import execution, planning
    from prismoid.schemas.plan import TaskStatus

    task = await task_ops.create_task(db, actor, goal="notes")
    await execution.advance(db, task.id, TaskStatus.PLANNING)
    view = await planning.draft_plan(db, task.id)
    await db.commit()

    body = b"# Notes\n\nThe first draft.\n"
    manifest = {
        "entries": [
            {
                "path": path,
                "type": "document",
                "title": "Notes",
                "sha256": storage.sha256(body),
                "bytes": len(body),
                "sources": [],
                "agent": "",
                "model": "",
                "skill_pins": [],
                "at": "2026-08-17T00:00:00Z",
            }
        ]
    }
    [artifact] = await ops.harvest(
        db,
        task.id,
        view.steps[0].id,
        Manifest.model_validate(manifest),
        {path: body},
        previews={path: preview} if preview is not None else None,
    )
    await db.commit()
    return artifact


async def test_a_text_document_revises_in_app(db: AsyncSession, actor: Actor) -> None:
    """The collection-only wall came down for text documents - full text in, next
    immutable revision out, user provenance, sidecars dropped (the content changed)."""
    base = await a_document_artifact(db, actor)
    revised = await ops.revise(
        db, actor, base.id, content="# Notes\n\nThe second draft.\n", base_sha256=base.sha256
    )
    assert (revised.lineage_id, revised.revision, revised.supersedes_id) == (base.lineage_id, 2, base.id)
    assert revised.provenance["agent"] == "user"
    assert revised.preview_sha256 is None and revised.rendition_sha256 is None
    assert revised.role == base.role, "a revision never changes WHAT the row is (working stays working)"


async def test_the_content_shape_pairs_with_the_kind(db: AsyncSession, actor: Actor) -> None:
    doc = await a_document_artifact(db, actor)
    with pytest.raises(ValidationFailed, match="full text"):
        await ops.revise(db, actor, doc.id, content=CONTENT, base_sha256=doc.sha256)
    with pytest.raises(ValidationFailed, match="erase"):
        await ops.revise(db, actor, doc.id, content="   ", base_sha256=doc.sha256)
    coll = await a_collection_artifact(db, actor)
    with pytest.raises(ValidationFailed, match="JSON"):
        await ops.revise(db, actor, coll.id, content="not an object", base_sha256=coll.sha256)


async def test_a_binary_document_still_refuses_text_edit(db: AsyncSession, actor: Actor) -> None:
    docx = await a_document_artifact(db, actor, path="/artifacts/report.docx")
    with pytest.raises(Conflict, match="restored"):
        await ops.revise(db, actor, docx.id, content="text over a zip", base_sha256=docx.sha256)


async def test_restore_mints_a_new_head_carrying_bytes_and_sidecars(db: AsyncSession, actor: Actor) -> None:
    """Restore-by-minting - the target's bytes AND its preview/rendition carry
    forward (identical bytes keep their sidecars honest), supersedes the HEAD, no re-upload."""
    v1 = await a_document_artifact(db, actor, preview=b'{"kind": "document", "blocks": []}')
    assert v1.preview_sha256 is not None
    v2 = await ops.revise(db, actor, v1.id, content="# Notes\n\nRewritten.\n", base_sha256=v1.sha256)

    restored = await ops.restore(db, actor, v1.id, base_sha256=v2.sha256)
    assert (restored.revision, restored.supersedes_id) == (3, v2.id), "supersedes the HEAD, not the target"
    assert restored.sha256 == v1.sha256 and restored.storage_key == v1.storage_key
    assert restored.preview_sha256 == v1.preview_sha256, "same bytes ⇒ the sidecar carries"
    assert restored.provenance["agent"] == "user"
    assert await Artifact.any_with_digest(db, v1.sha256), "two rows share the blob; GC sees both"

    latest = await Artifact.latest_for_task(db, v1.task_id)
    assert [a.id for a in latest] == [restored.id]


async def test_restore_guards_and_idempotence(db: AsyncSession, actor: Actor) -> None:
    v1 = await a_document_artifact(db, actor)
    v2 = await ops.revise(db, actor, v1.id, content="# v2\n", base_sha256=v1.sha256)

    with pytest.raises(Conflict, match=f"v{v2.revision}"):
        await ops.restore(db, actor, v1.id, base_sha256=v1.sha256)  # stale head token

    same = await ops.restore(db, actor, v2.id, base_sha256=v2.sha256)
    assert same.id == v2.id, "restoring the head's own bytes mints nothing (double-click safe)"

    stranger, _ = await member(db, "Bo")
    await db.commit()
    with pytest.raises(NotFound):
        await ops.restore(db, stranger, v1.id, base_sha256=v2.sha256)


async def test_restore_emits_the_revised_event(db: AsyncSession, actor: Actor) -> None:
    from prismoid.db.models.event import TaskEvent
    from prismoid.schemas.events import ArtifactRevised

    v1 = await a_document_artifact(db, actor)
    v2 = await ops.revise(db, actor, v1.id, content="# v2\n", base_sha256=v1.sha256)
    restored = await ops.restore(db, actor, v1.id, base_sha256=v2.sha256)

    events = [e for e in await TaskEvent.replay(db, v1.task_id) if isinstance(e, ArtifactRevised)]
    assert [e.artifact_id for e in events] == [v2.id, restored.id]
    assert events[-1].supersedes_id == v2.id, "the reducer repoints from the HEAD it replaces"


async def test_labels_set_trim_and_clear_with_revise_authz(db: AsyncSession, actor: Actor) -> None:
    base = await a_document_artifact(db, actor)
    row = await ops.set_label(db, actor, base.id, label="  final draft  ")
    assert row.label == "final draft"
    row = await ops.set_label(db, actor, base.id, label=None)
    assert row.label is None

    stranger, _ = await member(db, "Cy")
    await db.commit()
    with pytest.raises(NotFound):
        await ops.set_label(db, stranger, base.id, label="mine now")


async def test_a_follow_up_republish_chains_onto_the_parent_lineage(db: AsyncSession, actor: Actor) -> None:
    """The agent path onto the same spine: a follow-up that re-publishes the same filename is
    the deliverable's next revision, not a stranger with the same name."""
    from prismoid.operations import execution, planning
    from prismoid.schemas.plan import TaskStatus

    base = await a_collection_artifact(db, actor)
    parent_task_id = base.task_id

    child = await task_ops.create_task(db, actor, goal="refine it", parent_task_id=parent_task_id)
    await execution.advance(db, child.id, TaskStatus.PLANNING)
    child_view = await planning.draft_plan(db, child.id)
    await db.commit()

    body = orjson.dumps(CONTENT | {"title": "Workstreams, refined"})
    manifest = {
        "entries": [
            {
                "path": "/artifacts/work.collection.json",  # the SAME published basename
                "type": "collection",
                "title": "Workstreams, refined",
                "sha256": storage.sha256(body),
                "bytes": len(body),
                "sources": [],
                "agent": "",
                "model": "",
                "skill_pins": [],
                "at": "2026-07-31T01:00:00Z",
            }
        ]
    }
    [chained] = await ops.harvest(
        db,
        child.id,
        child_view.steps[0].id,
        Manifest.model_validate(manifest),
        {"/artifacts/work.collection.json": body},
    )
    await db.commit()

    assert chained.lineage_id == base.lineage_id
    assert chained.revision == 2
    assert chained.supersedes_id == base.id
    assert chained.task_id == child.id, "the revision belongs to the task that made it"
    assert uuid.UUID(str(chained.lineage_id)) == base.id


# ── the review pins: every fixed behavior wears its test ─────────────────────────────────────


async def test_an_identical_revision_is_a_no_op(db: AsyncSession, actor: Actor) -> None:
    """Byte-identical content mints nothing - restore's no-op law held on the revise door
    too, so an API replay or an unchanged save can never grow the lineage."""
    base = await a_document_artifact(db, actor)
    same = await ops.revise(
        db, actor, base.id, content="# Notes\n\nThe first draft.\n", base_sha256=base.sha256
    )
    assert (same.id, same.revision) == (base.id, 1), "nothing minted, the head returned as-is"


async def test_a_stale_save_with_an_invalid_draft_still_gets_the_409(db: AsyncSession, actor: Actor) -> None:
    """The stale-head 409 outranks content validation: the client's rebase flow keys on the
    409, and a 422-first would strand a stale tab on its old base forever."""
    v1 = await a_document_artifact(db, actor)
    v2 = await ops.revise(db, actor, v1.id, content="# v2\n", base_sha256=v1.sha256)
    with pytest.raises(Conflict, match=v2.sha256):
        await ops.revise(db, actor, v1.id, content="   ", base_sha256=v1.sha256)


async def test_a_racing_save_surfaces_the_designed_409_not_a_500(
    db: AsyncSession, actor: Actor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two same-base saves race past the head check (the window spans the upload await) -
    the (lineage_id, revision) unique index names the loser, and the door translates the
    IntegrityError into the documented 409 instead of an unhandled 500."""
    base = await a_document_artifact(db, actor)

    real_aput = storage.aput

    async def landing_rival(data: bytes, bucket: str | None = None) -> str:
        # The rival save lands DURING this save's upload window.
        rival_body = b"# Rival\n"
        db.add(
            Artifact(
                id=uuid.uuid4(),
                task_id=base.task_id,
                step_id=None,
                lineage_id=base.lineage_id,
                revision=base.revision + 1,
                supersedes_id=base.id,
                type=base.type,
                title=base.title,
                sha256=storage.sha256(rival_body),
                bytes=len(rival_body),
                storage_key=storage.key_for(storage.sha256(rival_body)),
                role=base.role,
                provenance=base.provenance,
            )
        )
        await db.flush()
        return await real_aput(data, bucket)

    monkeypatch.setattr(storage, "aput", landing_rival)
    with pytest.raises(Conflict, match="newer revision"):
        await ops.revise(db, actor, base.id, content="# Mine\n", base_sha256=base.sha256)


async def test_restoring_a_parent_made_revision_from_a_fork_stays_on_the_forks_task(
    db: AsyncSession, actor: Actor
) -> None:
    """A lineage can span tasks (a fork's republish chains onto the parent's spine with the
    fork's own task_id). Restore mints where the lineage's FACE lives - the HEAD's task -
    so restoring a parent-made v1 from the fork's history keeps the fork's shelf and event
    record intact instead of planting a surprise head on the settled parent."""
    from prismoid.db.models.event import TaskEvent
    from prismoid.operations import execution, planning
    from prismoid.schemas.events import ArtifactRevised
    from prismoid.schemas.plan import TaskStatus

    v1 = await a_collection_artifact(db, actor)

    child = await task_ops.create_task(db, actor, goal="refine it", parent_task_id=v1.task_id)
    await execution.advance(db, child.id, TaskStatus.PLANNING)
    child_view = await planning.draft_plan(db, child.id)
    await db.commit()
    body = orjson.dumps(CONTENT | {"title": "Workstreams, refined"})
    manifest = {
        "entries": [
            {
                "path": "/artifacts/work.collection.json",
                "type": "collection",
                "title": "Workstreams, refined",
                "sha256": storage.sha256(body),
                "bytes": len(body),
                "sources": [],
                "agent": "",
                "model": "",
                "skill_pins": [],
                "at": "2026-07-31T01:00:00Z",
            }
        ]
    }
    [v2] = await ops.harvest(
        db,
        child.id,
        child_view.steps[0].id,
        Manifest.model_validate(manifest),
        {"/artifacts/work.collection.json": body},
    )
    await db.commit()

    restored = await ops.restore(db, actor, v1.id, base_sha256=v2.sha256)
    assert restored.task_id == child.id, "the restore lands on the HEAD's task, never the parent's"
    assert (restored.revision, restored.supersedes_id) == (3, v2.id)
    child_events = [e for e in await TaskEvent.replay(db, child.id) if isinstance(e, ArtifactRevised)]
    assert [e.artifact_id for e in child_events] == [v2.id, restored.id], (
        "harvest chained v2 and the restore both ride the fork's record"
    )
    parent_events = [e for e in await TaskEvent.replay(db, v1.task_id) if isinstance(e, ArtifactRevised)]
    assert parent_events == [], "the settled parent's stream stays untouched"


async def test_history_rows_carry_their_verdicts(db: AsyncSession, actor: Actor) -> None:
    """The lineage endpoint serves each row's verdict (get_artifact's shape): a PASS-verified
    v1 must never read Pending in the history just because a human saved a v2."""
    from prismoid.api.endpoints import artifacts as artifacts_api
    from prismoid.db.models.verification import Verification

    v1 = await a_document_artifact(db, actor)
    db.add(Verification(artifact_id=v1.id, verdict="pass", suite="document", report={"checks": []}))
    await db.commit()
    v2 = await ops.revise(db, actor, v1.id, content="# v2\n", base_sha256=v1.sha256)

    page = await artifacts_api.list_revisions(v2.id, actor, db)
    by_id = {item.id: item.verdict for item in page.items}
    assert by_id[v1.id] == "pass", "the verified revision wears its verdict"
    assert by_id[v2.id] is None, "the fresh head honestly has none yet"


async def test_history_shows_only_revisions_on_tasks_the_reader_can_see(
    db: AsyncSession, actor: Actor
) -> None:
    """A lineage can span a fork that moved out of a Project: a teammate reading the parent's
    history gets the rows they can see, never the private fork's titles or hashes - and a save
    blocked by that hidden head is refused without naming it."""
    from prismoid.db.models.project import Project, Role
    from prismoid.db.models.project_member import ProjectMember
    from prismoid.db.models.task import Task
    from prismoid.operations import execution, planning
    from prismoid.schemas.plan import TaskStatus

    bea, _ = await member(db, "Bea")
    project = Project(name="shared", owner_actor_id=actor.id)
    db.add(project)
    await db.commit()
    db.add(ProjectMember(project_id=project.id, actor_id=bea.id, role=Role.EDITOR, added_by=actor.id))
    v1 = await a_collection_artifact(db, actor)
    parent = await db.get(Task, v1.task_id)
    assert parent is not None
    parent.project_id = project.id
    await db.commit()

    fork = await task_ops.create_task(db, actor, goal="refine it", parent_task_id=v1.task_id)
    fork.project_id = None  # moved home: private to Ana
    await execution.advance(db, fork.id, TaskStatus.PLANNING)
    fork_view = await planning.draft_plan(db, fork.id)
    await db.commit()
    body = orjson.dumps(CONTENT | {"title": "Private plans"})
    entry = {
        "path": "/artifacts/work.collection.json",
        "type": "collection",
        "title": "Private plans",
        "sha256": storage.sha256(body),
        "bytes": len(body),
        "sources": [],
        "agent": "",
        "model": "",
        "skill_pins": [],
        "at": "2026-07-31T01:00:00Z",
    }
    [v2] = await ops.harvest(
        db,
        fork.id,
        fork_view.steps[0].id,
        Manifest.model_validate({"entries": [entry]}),
        {"/artifacts/work.collection.json": body},
    )
    await db.commit()
    assert v2.lineage_id == v1.lineage_id

    assert [r.id for r in await ops.revisions(db, bea, v1.id)] == [v1.id]
    assert [r.id for r in await ops.revisions(db, actor, v1.id)] == [v2.id, v1.id]
    with pytest.raises(Conflict) as refused:
        await ops.revise(db, bea, v1.id, content=CONTENT, base_sha256=v1.sha256)
    assert v2.sha256 not in str(refused.value)
