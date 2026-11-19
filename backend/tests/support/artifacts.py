# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A published deliverable's row and its receipt, made the way harvest and the verify job make
them. A plain module, so a test imports it and never another test module."""

from __future__ import annotations

import uuid
from typing import Any

import orjson
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid import storage
from prismoid.db.models.actor import Actor
from prismoid.db.models.artifact import Artifact
from prismoid.db.session import session_scope
from prismoid.operations import artifacts as ops
from prismoid.operations import tasks as task_ops
from prismoid.operations import verification
from prismoid.schemas.artifacts import Manifest
from prismoid.schemas.verification import SignedVerification


def deliverable(
    task_id: uuid.UUID,
    body: bytes,
    *,
    step_id: uuid.UUID | None = None,
    kind: str = "document",
    path: str = "/artifacts/memo.md",
    title: str = "Memo",
    agent: str = "writer",
    model: str = "anthropic:claude-sonnet-5-5",
) -> Artifact:
    """The row harvest writes for published bytes: the bytes in the store, the provenance shaped
    like their manifest entry. Not added to any session: the caller puts it in its own."""
    digest = storage.put(body)
    return Artifact(
        id=uuid.uuid4(),
        lineage_id=uuid.uuid4(),
        task_id=task_id,
        step_id=step_id,
        type=kind,
        role="deliverable",
        title=title,
        sha256=digest,
        bytes=len(body),
        storage_key=storage.key_for(digest),
        provenance={
            "path": path,
            "type": kind,
            "title": title,
            "sha256": digest,
            "bytes": len(body),
            "sources": [],
            "agent": agent,
            "model": model,
            "skill_pins": [],
            "at": "2026-10-04T00:00:00Z",
        },
    )


async def verified(artifact_id: uuid.UUID, workspace_id: uuid.UUID) -> SignedVerification:
    """The receipt, signed as the verify job signs it: the row read back in its workspace's
    session, then committed."""
    async with session_scope(workspace_id=workspace_id) as s:
        row = await s.get(Artifact, artifact_id)
        assert row is not None
        signed = await verification.verify_artifact(s, row)
        await s.commit()
    return signed


CONTENT: dict[str, Any] = {
    "collection_version": 1,
    "title": "Workstreams",
    "fields": [
        {"key": "name", "label": "Name", "type": "text"},
        {"key": "stage", "label": "Stage", "type": "select", "options": ["Todo", "Done"]},
    ],
    "rows": [{"id": "r1", "cells": {"name": "Line expansion", "stage": "Todo"}}],
    "views": [{"name": "Board", "layout": "board", "group_by": "stage"}],
}


async def a_collection_artifact(db: AsyncSession, actor: Actor) -> Artifact:
    from prismoid.operations import execution, planning
    from prismoid.schemas.plan import TaskStatus

    task = await task_ops.create_task(db, actor, goal="organize")
    await execution.advance(db, task.id, TaskStatus.PLANNING)
    view = await planning.draft_plan(db, task.id)
    await db.commit()

    body = orjson.dumps(CONTENT)
    manifest = {
        "entries": [
            {
                "path": "/artifacts/work.collection.json",
                "type": "collection",
                "title": "Workstreams",
                "sha256": storage.sha256(body),
                "bytes": len(body),
                "sources": [],
                "agent": "",
                "model": "",
                "skill_pins": [],
                "at": "2026-07-31T00:00:00Z",
            }
        ]
    }
    [artifact] = await ops.harvest(
        db,
        task.id,
        view.steps[0].id,
        Manifest.model_validate(manifest),
        {"/artifacts/work.collection.json": body},
    )
    await db.commit()
    return artifact
