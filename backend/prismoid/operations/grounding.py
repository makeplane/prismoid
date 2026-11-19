# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Claims grounding for a document's receipt: one bounded claim reading, then code checks every claim.

The sources are the declared files under their names and each page the task kept under the source id
the task gave it (`[S3]`, `operations/sources.py`), so a citation resolves to the stored text that was
read. `claim_extract` reads the document and those sources once per reading; a long-form deliverable is
read from its parts (they keep the task's `[S#]` ids), packed whole into readings of the reader's bound, so
the whole book is read once and no call holds more than one bound. `ai/verification/grounding.py` decides.
Fail-soft: a reader that does not answer leaves a SKIP that says so, never a FAIL on the author.
"""

from __future__ import annotations

import uuid

import logfire
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid import storage
from prismoid.ai.agents import base as agents
from prismoid.ai.agents.run import AgentRunner
from prismoid.ai.deps import TaskDeps
from prismoid.ai.tools.labels import fetch_label
from prismoid.ai.verification import grounding
from prismoid.db.models.artifact import Artifact
from prismoid.db.models.plan import PlanEdge, PlanStep
from prismoid.db.models.task import Task
from prismoid.errors import NotFound, PrismoidError
from prismoid.operations import models as model_ops
from prismoid.operations import routing as routing_ops
from prismoid.operations import sources as sources_ops
from prismoid.operations import usage as usage_ops
from prismoid.operations.deadline import hard_timeout
from prismoid.schemas.claims import GROUNDING, ClaimFinding, ClaimSet, Grounding
from prismoid.schemas.ids import ProjectId, StepId, TaskId
from prismoid.schemas.outputs import VerifierSuite
from prismoid.schemas.plan import StepSpec
from prismoid.schemas.verification import Check

READER_TIMEOUT_S = 90.0  # as the receipt judge's: a reader silent this long is wedged


async def ground(
    session: AsyncSession,
    artifact: Artifact,
    suite: VerifierSuite,
    document: str | None,
    *,
    files: dict[str, bytes],
    pages: list[grounding.Source],
) -> list[Check]:
    """The `grounding:claims` check for a document on the claims path, else nothing. Commits nothing."""
    if suite is not VerifierSuite.DOCUMENT or GROUNDING is not Grounding.CLAIMS or document is None:
        return []  # not this path, or the suite never read the document and its own checks say why
    task = await session.get(Task, artifact.task_id)
    if task is None:
        raise NotFound("no such task")
    if not document.strip():
        return [grounding.skipped("no document text to read claims from")]
    sources = [*_files(files), *pages]
    if not sources:
        return [grounding.skipped("no readable sources - nothing to ground against")]
    try:
        runner = await _reader(session, task, artifact)
    except Exception as failed:  # fail-soft: the reader is an aid; without it the receipt says unchecked
        return [grounding.skipped(f"the claim reader did not answer ({type(failed).__name__})")]
    findings: list[ClaimFinding] = []
    unread = ""
    for window in grounding.windows(await parts(session, artifact) or [document]):
        reading = grounding.reading(window, sources)
        claims = await _claims(runner, reading.prompt)
        if isinstance(claims, str):
            unread = claims
            continue
        findings += grounding.ground(claims.claims, sources, clipped=reading.clipped, document=window)
    if unread and not any(f.status.failed for f in findings):  # a defect found elsewhere still stands
        return [grounding.skipped(f"the claim reader did not answer ({unread})")]
    return [grounding.check(findings)]


async def _reader(session: AsyncSession, task: Task, artifact: Artifact) -> AgentRunner:
    """The claim reader, metered on this session; raises when no model can seat it."""
    deps = TaskDeps(
        task_id=TaskId(task.id),
        step_id=StepId(artifact.step_id) if artifact.step_id else None,
        project_id=ProjectId(task.project_id) if task.project_id else None,
        frame=None,
        emit_usage=usage_ops.emitter(session, own_transaction=True),  # never the caller's connection
    )
    catalog, order = await model_ops.routed_catalog(session)
    settings = await routing_ops.settings_for(session)
    return agents.build("claim_extract", deps, catalog, order=order, settings=settings)


async def _claims(runner: AgentRunner, prompt: str) -> ClaimSet | str:
    """One reading's claims, or the kind of failure that kept the reader from answering."""
    try:
        result = await hard_timeout(runner.run(prompt), READER_TIMEOUT_S)
    except Exception as failed:  # fail-soft: the reader is an aid; without it the receipt says unchecked
        logfire.warn("the claim reader did not answer ({kind})", kind=type(failed).__name__)
        return type(failed).__name__
    if not isinstance(result.output, ClaimSet):
        raise PrismoidError(f"the claim reader answered a {type(result.output).__name__}, not a claim set")
    return result.output


async def parts(session: AsyncSession, artifact: Artifact) -> list[str]:
    """The text of each part a long-form deliverable was assembled from, in document order; [] for any other.
    The parts keep the task's own `[S#]` ids, which assembly renumbers."""
    step = await session.get(PlanStep, artifact.step_id) if artifact.step_id else None
    if step is None or StepSpec.model_validate(step.spec).lane_role != "assembly":
        return []
    fed = {
        e.from_step_id for e in await PlanEdge.for_task(session, artifact.task_id) if e.to_step_id == step.id
    }
    order = {
        s.id: s.ordinal
        for s in await PlanStep.for_task(session, artifact.task_id)
        if s.id in fed and StepSpec.model_validate(s.spec).lane_role == "section"
    }
    newest = {
        a.step_id: a
        for a in await Artifact.for_task(session, artifact.task_id)
        if a.step_id in order and a.role != "superseded"
    }
    ordered = sorted(newest.values(), key=lambda a: order[a.step_id] if a.step_id else 0)
    return [(await storage.aget(a.sha256)).decode("utf-8", errors="replace") for a in ordered]


async def kept(session: AsyncSession, task_id: uuid.UUID) -> list[grounding.Source]:
    """Each page the task kept, under every source id and URL it was given, else its frame name."""
    named: dict[str, list[tuple[str, str]]] = {}
    for sid, url in (await sources_ops.source_ids(task_id)).items():
        named.setdefault(fetch_label(url), []).append((sid, url))
    keeper = sources_ops.keeper(task_id)
    texts: dict[str, tuple[str, list[str]]] = {}
    for page in await sources_ops.stored(session, task_id):
        text = await keeper.read(page.sha256)
        for label, locator in named.get(page.page) or [
            (f"{sources_ops.PAGE_PREFIX}{page.sha256[:16]}.md", page.page)
        ]:
            texts.setdefault(label, (locator, []))[1].append(text)
    return [grounding.Source(label, "\n\n".join(parts), locator) for label, (locator, parts) in texts.items()]


def _files(files: dict[str, bytes]) -> list[grounding.Source]:
    """The declared files that are text, under their names; a binary file is the judge's to read."""
    out: list[grounding.Source] = []
    for name, data in files.items():
        try:
            out.append(grounding.Source(name, data.decode("utf-8"), name))
        except UnicodeDecodeError:
            continue
    return out
