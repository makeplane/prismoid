# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Every artifact shape, one file. `*Out` = response."""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from prismoid.schemas.artifacts import ArtifactView
from prismoid.schemas.verification import SignedVerification


class ArtifactOut(BaseModel):
    """An artifact, with its provenance chain.

    `provenance` is the manifest entry, verbatim: who made it, with which model, from which
    sources, pinned to which skill versions. It is the chain that makes a claim *checkable* -
    without it, a download is a file somebody swears is right.
    """

    model_config = ConfigDict(frozen=True)

    id: UUID
    task_id: UUID  # the owning task - an artifact is only ever reached through it
    type: str
    title: str
    filename: str  # the published file's basename - clients render by extension, never by guess
    render: str  # RenderHint wire value: "markdown" | "text" | "collection" | "dataset" | "pdf" | "change" | "none"
    lineage_id: UUID  # the revision chain's identity
    revision: int  # 1 = as published; each save mints the next
    label: str | None  # the user's name for THIS revision ("final draft")
    has_preview: bool  # a render sidecar exists - GET /preview
    has_rendition: bool  # a full-fidelity rendition exists - GET /rendition
    rendition_media: str | None  # "application/pdf" | "text/html" - how a client shows it
    sha256: str
    bytes: int
    provenance: dict[str, Any]
    verdict: str | None
    created_at: dt.datetime


class SheetOut(BaseModel):
    """One worksheet's head, with COMPUTED values - generated in the frame by the same kind of
    evaluator that verifies the workbook (harvest-time)."""

    model_config = ConfigDict(frozen=True)

    name: str
    dims: str
    rows: list[list[str | float | int | None]]
    truncated: bool = False


class DocBlockOut(BaseModel):
    """One extracted document block: a styled line (`style` ∈ h1|h2|h3|li|p) OR a table."""

    model_config = ConfigDict(frozen=True)

    style: str | None = None
    text: str | None = None
    table: list[list[str]] | None = None


class SlideOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    title: str
    lines: list[str] = []


class CodeFileOut(BaseModel):
    """One file in a code archive. `text` is present only for small UTF-8 members."""

    model_config = ConfigDict(frozen=True)

    path: str
    size: int
    text: str | None = None


class PreviewOut(BaseModel):
    """The render sidecar: ONE `kind` discriminator, one payload
    field per kind - sheet grids, document blocks, deck slides, code listings, or a page's own
    HTML (rendered client-side in a fully sandboxed iframe; the server only carries it)."""

    model_config = ConfigDict(frozen=True)

    kind: str  # "sheet" | "document" | "deck" | "code" | "page"
    sheets: list[SheetOut] = []
    blocks: list[DocBlockOut] = []
    slides: list[SlideOut] = []
    files: list[CodeFileOut] = []
    html: str | None = None
    truncated: bool = False


class ReviseIn(BaseModel):
    """A save from the editor: the FULL next content plus the sha it was edited from.

    Full content, not a patch - a collection is small, CAS dedupes unchanged bytes, and a patch
    format is a merge engine nobody asked for. The content's shape pairs with the kind:
    a collection sends the object; a text document (.md/.txt) sends the string -
    the operation refuses the wrong pairing with the right shape named. `base_sha256` is the
    optimistic-concurrency token: stale ⇒ 409 naming the newer sha, and the client rebases
    its staged edits.
    """

    content: dict[str, Any] | str
    base_sha256: str


class RestoreIn(BaseModel):
    """Restore-by-minting: the path names the revision to bring back; the body
    carries the CURRENT HEAD's sha - the same optimistic-concurrency shape as revise, so two
    racing restores cannot silently interleave."""

    base_sha256: str


class LabelIn(BaseModel):
    """Name (or un-name) a revision. Null clears; the operation trims and caps."""

    label: str | None = None


class DownloadOut(BaseModel):
    """A presigned link, signed for the public endpoint - never an internal hostname."""

    url: str
    filename: str
    sha256: str
    expires_in: int


class CheckOut(BaseModel):
    """One deterministic assertion, and what it found. `expected`/`actual` are the whole point:
    *"numbers:grounded failed"* is a shrug; *"9.9 is not in any source"* is a fix."""

    model_config = ConfigDict(frozen=True)

    name: str
    status: str
    detail: str = ""
    expected: str | None = None
    actual: str | None = None


class VerificationOut(BaseModel):
    """The signed report - what `prismoid artifacts verify` prints.

    `signature_valid` is **recomputed at read time**, not read from a column. A stored boolean
    saying "this is valid" is a boolean somebody can UPDATE; recomputing it from the canonical bytes
    is the only thing an auditor could ever trust, and it is what makes the tamper test possible.
    """

    model_config = ConfigDict(frozen=True)

    artifact_id: UUID
    artifact_sha256: str
    suite: str
    verdict: str
    checks: list[CheckOut]

    producer_model: str
    producer_family: str
    verifier_model: str | None = None
    verifier_family: str | None = None
    judge_rationale: str | None = None

    signature: str
    public_key: str
    algorithm: str

    # **Three states, not two.** `None` = not signed yet; `True` = signed and the signature
    # verifies; `False` = signed and it does **not** - someone edited the report. Collapsing the
    # first two into a bool would make an unsigned report look tampered with, and collapsing the
    # last two would make a tampered one look merely unfinished. Both are lies of exactly the kind
    # this milestone exists to prevent.
    signature_valid: bool | None

    attempt: int
    at: dt.datetime


def verification_out(
    artifact_id: UUID, signed: SignedVerification, signature_valid: bool | None
) -> VerificationOut:
    report = signed.report
    return VerificationOut(
        artifact_id=artifact_id,
        artifact_sha256=report.artifact_sha256,
        suite=report.suite.value,
        verdict=report.verdict.value,
        checks=[CheckOut.model_validate(c.model_dump()) for c in report.checks],
        producer_model=report.producer_model,
        producer_family=str(report.producer_family),
        verifier_model=report.judge.model if report.judge else None,
        verifier_family=str(report.judge.family) if report.judge else None,
        judge_rationale=report.judge.rationale if report.judge else None,
        signature=signed.signature,
        public_key=signed.public_key,
        algorithm=signed.algorithm,
        signature_valid=signature_valid,
        attempt=report.attempt,
        at=report.at,
    )


def artifact_out(view: ArtifactView) -> ArtifactOut:
    return ArtifactOut(
        id=view.id,
        task_id=view.task_id,
        type=view.type.value,
        title=view.title,
        filename=view.filename,
        render=view.render.value,
        lineage_id=view.lineage_id,
        revision=view.revision,
        label=view.label,
        has_preview=view.has_preview,
        has_rendition=view.has_rendition,
        rendition_media=view.rendition_media,
        sha256=view.sha256,
        bytes=view.bytes,
        provenance=view.provenance.model_dump(mode="json"),
        verdict=view.verdict.value if view.verdict else None,
        created_at=view.created_at,
    )
