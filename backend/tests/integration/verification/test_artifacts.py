# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Publish → harvest → provenance.

The property under test is the one the whole trust story rests on: **an artifact exists only
because it went through publish**, and it carries a chain that says who made it, with what, from
what. A download that cannot answer those three questions is a file somebody swears is right.

Needs the stack (`just up`) and Docker.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid import storage
from prismoid.ai.frames.docker_driver import DockerFrame, ensure_image
from prismoid.db.models.actor import Actor
from prismoid.db.models.artifact import Artifact
from prismoid.db.session import session_scope
from prismoid.errors import NotFound, PolicyDenied
from prismoid.operations import artifacts as ops
from prismoid.operations import tasks as task_ops
from prismoid.schemas.artifacts import Manifest

# frame-spinning suite: ride a serialization lane like every sibling that boots Docker
# frames - unmarked, these escaped the justfile's three-lane loadgroup.
from tests.support.cast import member

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("frames-c")]


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    """Just a session. Cleanup is autouse in `tests/conftest.py`, scoped to what this test made -
    a suite that nukes `actors` also nukes the developer's login."""
    async with session_scope() as session:
        yield session


@pytest.fixture
async def actor(db: AsyncSession) -> Actor:
    row, _ = await member(db, "Ana")
    await db.commit()
    return row


async def a_task_with_a_step(db: AsyncSession, actor: Actor) -> tuple[uuid.UUID, uuid.UUID]:
    """A real task and a **real** plan step.

    `artifacts.step_id` is a foreign key onto `plan_steps`, and the first version of these tests
    used a fabricated UUID - which the database refused, correctly. An artifact that points at a
    step that never existed has no provenance chain, and provenance is the entire product.
    """
    from prismoid.operations import execution, planning
    from prismoid.schemas.plan import TaskStatus

    task = await task_ops.create_task(db, actor, goal="g")
    await execution.advance(db, task.id, TaskStatus.PLANNING)
    view = await planning.draft_plan(db, task.id)
    await db.commit()
    return task.id, view.steps[0].id


async def test_publish_is_the_only_door(db: AsyncSession, actor: Actor) -> None:
    """A file dropped into /artifacts without publishing has no manifest entry - so harvest does
    not see it. **Harvest reads the manifest, not a directory listing**, and that is what makes
    Rule 9 a mechanism rather than a hope."""
    ensure_image()
    frame = DockerFrame(task_id=f"t-{uuid.uuid4().hex[:8]}")
    await frame.create(allow_domains=[])
    try:
        # Smuggled in - no publish, no manifest entry.
        await frame.exec("echo 'i was never published' > /artifacts/smuggled.md")
        # Properly published.
        await frame.write_file("/work/memo.md", "# Memo\n\nverification beats vibes.\n")
        await frame.publish("/work/memo.md", "document", "A memo")

        manifest = await frame.manifest()
        paths = [e["path"] for e in manifest["entries"]]
        assert paths == ["/artifacts/memo.md"]
        assert "/artifacts/smuggled.md" not in paths
    finally:
        await frame.destroy()


async def test_the_door_refuses_a_type_it_does_not_know(db: AsyncSession) -> None:
    """The type check is at the door on purpose. An agent that publishes a `.txt` as a
    `spreadsheet` would send it to the spreadsheet verifier, which would report a corrupt workbook
    - a confusing failure two subsystems away from the mistake."""
    ensure_image()
    frame = DockerFrame(task_id=f"t-{uuid.uuid4().hex[:8]}")
    await frame.create(allow_domains=[])
    try:
        await frame.write_file("/work/memo.md", "# Memo\n")
        with pytest.raises(PolicyDenied, match="not a valid spreadsheet"):
            await frame.publish("/work/memo.md", "spreadsheet", "nope")
        with pytest.raises(PolicyDenied, match="unknown artifact type"):
            await frame.publish("/work/memo.md", "markdown", "nope")
    finally:
        await frame.destroy()


async def test_harvest_stores_content_addressed_and_is_idempotent(db: AsyncSession, actor: Actor) -> None:
    """The key IS the hash. The same bytes published twice cost one object, and a download can
    never disagree with the verification that signed it."""
    task_id, step_id = await a_task_with_a_step(db, actor)
    body = b"# Memo\n\nverification beats vibes.\n"
    digest = storage.sha256(body)

    manifest = {
        "entries": [
            {
                "path": "/artifacts/memo.md",
                "type": "document",
                "title": "A memo",
                "sha256": digest,
                "bytes": len(body),
                "sources": [],
                "agent": "",
                "model": "",
                "skill_pins": [],
                "at": "2026-07-14T00:00:00Z",
            }
        ]
    }

    first = await ops.harvest(
        db, task_id, step_id, Manifest.model_validate(manifest), {"/artifacts/memo.md": body}
    )
    await db.commit()
    assert len(first) == 1
    assert first[0].sha256 == digest
    assert storage.get(digest) == body

    # Harvest again - same bytes, same hash, no duplicate row.
    again = await ops.harvest(
        db, task_id, step_id, Manifest.model_validate(manifest), {"/artifacts/memo.md": body}
    )
    await db.commit()
    assert again[0].id == first[0].id
    assert len(await Artifact.for_task(db, task_id)) == 1


async def test_another_step_publishing_the_same_bytes_gets_its_own_row(
    db: AsyncSession, actor: Actor
) -> None:
    """Idempotent per step, never across steps. A long-form follow-up's writer told
    to keep a section reproduced it byte for byte, harvest handed it the LAST turn's row, and
    the seam pass after it - which finds a step's output by that step - mounted nothing and
    published its impasse as the section."""
    from prismoid.db.models.plan import PlanEdge, PlanStep
    from prismoid.db.models.task import Task
    from prismoid.operations.execution.mounts import upstream_artifacts

    task_id, first = await a_task_with_a_step(db, actor)
    keeper = PlanStep(task_id=task_id, ordinal=1, title="01 Threat model")
    reader = PlanStep(task_id=task_id, ordinal=2, title="01 Threat model - seam pass")
    db.add_all([keeper, reader])
    await db.flush()
    db.add(PlanEdge(task_id=task_id, from_step_id=keeper.id, to_step_id=reader.id))
    body = b"## Threat model\n\nKept verbatim.\n"
    entry = {
        "path": "/artifacts/01-threat-model.md",
        "type": "document",
        "title": "01 Threat model",
        "sha256": storage.sha256(body),
        "bytes": len(body),
        "sources": [],
        "agent": "",
        "model": "",
        "skill_pins": [],
        "at": "2026-10-06T00:00:00Z",
    }
    manifest = Manifest.model_validate({"entries": [entry]})
    payloads = {"/artifacts/01-threat-model.md": body}

    [earlier] = await ops.harvest(db, task_id, first, manifest, payloads)
    [kept] = await ops.harvest(db, task_id, keeper.id, manifest, payloads)
    await db.commit()

    assert kept.id != earlier.id and kept.step_id == keeper.id and kept.sha256 == earlier.sha256
    task = await db.get(Task, task_id)
    assert task is not None
    assert [a.id for a in await upstream_artifacts(db, task, reader)] == [kept.id]


async def test_harvest_births_the_row_with_its_preview(db: AsyncSession, actor: Actor) -> None:
    """The render sidecar arrives WITH the artifact, not after the verify job - the
    reader that auto-opens on `artifact.produced` must find `has_preview` already true. And a
    re-publish of identical bytes may bring the sidecar a first (pre-022) harvest lacked."""
    task_id, step_id = await a_task_with_a_step(db, actor)
    body = b"PK\x03\x04 pretend workbook"
    digest = storage.sha256(body)
    manifest = {
        "entries": [
            {
                "path": "/artifacts/model.xlsx",
                "type": "spreadsheet",
                "title": "A model",
                "sha256": digest,
                "bytes": len(body),
                "sources": [],
                "agent": "",
                "model": "",
                "skill_pins": [],
                "at": "2026-07-14T00:00:00Z",
            }
        ]
    }
    payloads = {"/artifacts/model.xlsx": body}

    bare = await ops.harvest(db, task_id, step_id, Manifest.model_validate(manifest), payloads)
    await db.commit()
    assert bare[0].preview_sha256 is None  # no sidecar handed in - honest NULL

    sidecar = b'{"kind": "sheet", "sheets": []}'
    render = b"%PDF-1.7 pretend faithful render"
    again = await ops.harvest(
        db,
        task_id,
        step_id,
        Manifest.model_validate(manifest),
        payloads,
        previews={"/artifacts/model.xlsx": sidecar},
        renditions={"/artifacts/model.xlsx": (render, "application/pdf")},
    )
    await db.commit()
    assert again[0].id == bare[0].id  # idempotent by hash, as ever
    pointer = again[0].preview_sha256
    assert pointer == storage.sha256(sidecar)  # …and now it carries the sidecar
    assert pointer is not None and storage.get(pointer) == sidecar
    # The rendition rides the same backfill: its own CAS blob + the media that says
    # how a client shows it.
    r_pointer = again[0].rendition_sha256
    assert r_pointer == storage.sha256(render)
    assert again[0].rendition_media == "application/pdf"
    assert r_pointer is not None and storage.get(r_pointer) == render


async def test_get_rendition_serves_a_native_pdf_as_its_own_bytes(db: AsyncSession, actor: Actor) -> None:
    """A `.pdf` never gets a LibreOffice rendition - the bytes ARE the rendition, served on the
    same inline lane with the artifact's OWN sha (the endpoint guard used to 404 exactly this,
    blanking the reader for every native PDF)."""
    from prismoid.api.endpoints.artifacts import get_rendition
    from prismoid.errors import NotFound

    task_id, step_id = await a_task_with_a_step(db, actor)
    body = b"%PDF-1.7 the deliverable itself"
    entry = {
        "path": "/artifacts/report.pdf",
        "type": "document",
        "title": "A report",
        "sha256": storage.sha256(body),
        "bytes": len(body),
        "sources": [],
        "agent": "",
        "model": "",
        "skill_pins": [],
        "at": "2026-07-14T00:00:00Z",
    }
    manifest = Manifest.model_validate({"entries": [entry]})
    rows = await ops.harvest(db, task_id, step_id, manifest, {"/artifacts/report.pdf": body})
    await db.commit()

    out = await get_rendition(rows[0].id, actor, db)
    assert out.sha256 == rows[0].sha256, "its own bytes, its own digest"
    assert out.filename == "report.pdf"
    assert "report.pdf" in out.url

    # …while a kind with no rendition lane at all still says so honestly.
    entry_md = dict(entry, path="/artifacts/memo.md", sha256=storage.sha256(b"# hi"), bytes=4)
    rows_md = await ops.harvest(
        db,
        task_id,
        step_id,
        Manifest.model_validate({"entries": [entry_md]}),
        {"/artifacts/memo.md": b"# hi"},
    )
    await db.commit()
    with pytest.raises(NotFound):
        await get_rendition(rows_md[0].id, actor, db)


async def test_a_hash_mismatch_is_refused(db: AsyncSession, actor: Actor) -> None:
    """The frame hashed it; we hash it again. If they disagree, the bytes changed between publish
    and harvest - a bug or an attack, and neither is a thing to shrug at. PolicyDenied since
    A corrupt publish is a failed ATTEMPT the ladder retries, never a dead thread."""
    task_id, step_id = await a_task_with_a_step(db, actor)
    manifest = {
        "entries": [
            {
                "path": "/artifacts/memo.md",
                "type": "document",
                "title": "A memo",
                "sha256": "0" * 64,  # a lie
                "bytes": 4,
                "sources": [],
                "agent": "",
                "model": "",
                "skill_pins": [],
                "at": "2026-07-14T00:00:00Z",
            }
        ]
    }
    with pytest.raises(PolicyDenied, match="hash mismatch"):
        await ops.harvest(
            db, task_id, step_id, Manifest.model_validate(manifest), {"/artifacts/memo.md": b"real"}
        )


async def test_an_artifact_is_scoped_through_its_task(db: AsyncSession, actor: Actor) -> None:
    """An artifact belongs to a Project *through* its Task (there is deliberately no second
    parent). So `get_for` scopes through the Task, and a stranger gets NotFound, not Forbidden."""
    task_id, step_id = await a_task_with_a_step(db, actor)
    body = b"secret"
    manifest = {
        "entries": [
            {
                "path": "/artifacts/x.md",
                "type": "document",
                "title": "x",
                "sha256": storage.sha256(body),
                "bytes": len(body),
                "sources": [],
                "agent": "",
                "model": "",
                "skill_pins": [],
                "at": "2026-07-14T00:00:00Z",
            }
        ]
    }
    [artifact] = await ops.harvest(
        db, task_id, step_id, Manifest.model_validate(manifest), {"/artifacts/x.md": body}
    )
    await db.commit()

    stranger, _ = await member(db, "Bo")
    await db.commit()

    assert (await Artifact.get_for(db, artifact.id, actor)).id == artifact.id
    with pytest.raises(NotFound):
        await Artifact.get_for(db, artifact.id, stranger)


async def test_harvest_stamps_the_plan_skill_pins_into_provenance(db: AsyncSession, actor: Actor) -> None:
    """`ManifestEntry.skill_pins` existed and was always `[]` - both frame drivers honestly write
    an empty list because the frame does not know what shaped the work. The PLAN knows: pins were
    resolved to exact versions at submit and copied onto the step. Harvest stamps them beside
    `agent` and `model`, which turns "pinned to which skill versions" from a schema promise into
    provenance."""
    from prismoid.db.models.plan import PlanStep

    task_id, step_id = await a_task_with_a_step(db, actor)
    step = await db.get(PlanStep, step_id)
    assert step is not None
    step.spec = {**step.spec, "skill_pins": [{"name": "report", "version": 3}]}
    await db.commit()

    body = b"# pinned\n"
    manifest = {
        "entries": [
            {
                "path": "/artifacts/pinned.md",
                "type": "document",
                "title": "pinned",
                "sha256": storage.sha256(body),
                "bytes": len(body),
                "sources": [],
                "agent": "",
                "model": "",
                "skill_pins": [],  # the frame's honest "don't know"
                "at": "2026-07-14T00:00:00Z",
            }
        ]
    }
    [artifact] = await ops.harvest(
        db, task_id, step_id, Manifest.model_validate(manifest), {"/artifacts/pinned.md": body}
    )
    await db.commit()

    assert artifact.provenance["skill_pins"] == [{"name": "report", "version": 3}]
