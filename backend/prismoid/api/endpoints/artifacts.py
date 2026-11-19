# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Artifacts - created only by publish, delivered with their verification.

Thin. `Artifact.get_for` is the only fetch path and it scopes through the **Task** -
because an artifact belongs to a Project *through* its Task, and there is deliberately no second
parent.
"""

from __future__ import annotations

from uuid import UUID

from prismoid.api.deps import ActorDep, SessionDep
from prismoid.api.serializers.artifacts import (
    ArtifactOut,
    DownloadOut,
    LabelIn,
    PreviewOut,
    RestoreIn,
    ReviseIn,
    VerificationOut,
    artifact_out,
    verification_out,
)
from prismoid.api.serializers.common import PageOut
from prismoid.db.models.artifact import Artifact
from prismoid.errors import NotFound
from prismoid.operations import artifacts as ops
from prismoid.operations import verification as verify_ops


async def get_artifact(artifact_id: UUID, actor: ActorDep, session: SessionDep) -> ArtifactOut:
    row = await Artifact.get_for(session, artifact_id, actor)
    verdict = await verify_ops.verdict_for(session, row.id)
    return artifact_out(ops.view(row, verdict=verdict))


async def download_artifact(artifact_id: UUID, actor: ActorDep, session: SessionDep) -> DownloadOut:
    row = await Artifact.get_for(session, artifact_id, actor)
    filename = str(row.provenance.get("path", row.title)).rsplit("/", 1)[-1]
    return DownloadOut(url=ops.download_url(row), filename=filename, sha256=row.sha256, expires_in=3600)


async def get_rendition(artifact_id: UUID, actor: ActorDep, session: SessionDep) -> DownloadOut:
    """The full-fidelity rendition: docx/pptx as PDF, xlsx as styled HTML - and a
    native `.pdf` serves its own bytes on the same inline lane (`rendition_sha256` NULL there)."""
    row = await Artifact.get_for(session, artifact_id, actor)
    url = ops.rendition_url(row)
    if url is None:
        raise NotFound("this artifact has no rendition")
    ext = "pdf" if (row.rendition_media or "application/pdf") == "application/pdf" else "html"
    stem = str(row.provenance.get("path", row.title)).rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return DownloadOut(
        url=url, filename=f"{stem}.{ext}", sha256=row.rendition_sha256 or row.sha256, expires_in=3600
    )


async def revise_artifact(
    artifact_id: UUID, body: ReviseIn, actor: ActorDep, session: SessionDep
) -> ArtifactOut:
    """A human edit saved as the lineage's next immutable revision."""
    row = await ops.revise(session, actor, artifact_id, content=body.content, base_sha256=body.base_sha256)
    return artifact_out(ops.view(row))


async def list_revisions(artifact_id: UUID, actor: ActorDep, session: SessionDep) -> PageOut[ArtifactOut]:
    """The whole lineage, newest first - the version picker's list (bounded; no cursor).
    Verdicts ride along (get_artifact's shape): a PASS-verified v1 must never read Pending
    in the history just because a human saved a v2."""
    rows = await ops.revisions(session, actor, artifact_id)
    items = [artifact_out(ops.view(r, verdict=await verify_ops.verdict_for(session, r.id))) for r in rows]
    return PageOut(items=items, next_cursor=None)


async def restore_artifact(
    artifact_id: UUID, body: RestoreIn, actor: ActorDep, session: SessionDep
) -> ArtifactOut:
    """Restore-by-minting: the id names the revision to bring back; a new head
    carries its bytes. Every kind, binaries included."""
    row = await ops.restore(session, actor, artifact_id, base_sha256=body.base_sha256)
    return artifact_out(ops.view(row))


async def label_artifact(
    artifact_id: UUID, body: LabelIn, actor: ActorDep, session: SessionDep
) -> ArtifactOut:
    """Name a revision - "final draft" in the history picker; null clears."""
    row = await ops.set_label(session, actor, artifact_id, label=body.label)
    return artifact_out(ops.view(row))


async def get_preview(artifact_id: UUID, actor: ActorDep, session: SessionDep) -> PreviewOut:
    """The render sidecar - computed sheet heads for a binary artifact; 404 = none."""
    return PreviewOut.model_validate(await ops.preview(session, actor, artifact_id))


async def get_verification(artifact_id: UUID, actor: ActorDep, session: SessionDep) -> VerificationOut:
    """The report, and its signature **re-checked right now** (the tamper test).

    A 404 here means *not verified*, which is not the same as `verdict: unverified` - the first is
    "nobody has looked", the second is "we looked and could prove nothing". Conflating them is how a
    system starts telling people an unchecked file is fine.
    """
    row = await Artifact.get_for(session, artifact_id, actor)  # scope first; the report is not a bypass
    signed = await verify_ops.latest_for(session, row.id)
    if signed is None:
        raise NotFound("this artifact has not been verified")
    return verification_out(row.id, signed, verify_ops.signature_valid(signed))
