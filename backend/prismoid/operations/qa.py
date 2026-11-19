# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The QA gate's composer (ADR-026) - a fresh frame, the pre-checks, the read, the verdict.

`verification.py`'s shape, editorial teeth: DB + frame + model meet HERE and nowhere else.
Every infrastructure failure degrades - a broken QA pass costs the gate (PASS_UNREAD, note
showing), never the thread. The lane's receipt signs only after delivery (ADR-019), so this
gate is the lane's ONLY check before it, and an unread pass is logged loudly every time: a
persistently unread gate is a product regression, not a footnote.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import logfire
import orjson
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.deps import TaskDeps
from prismoid.ai.frames.driver import ExecResult, FrameDriver, run_python
from prismoid.ai.verification import pipeline, qa_outcome
from prismoid.db.base import utcnow
from prismoid.db.models.artifact import Artifact
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.operations import machines, verification
from prismoid.operations import usage as usage_ops
from prismoid.schemas import sizing
from prismoid.schemas.events import QAReviewed
from prismoid.schemas.ids import ProjectId, StepId, TaskId
from prismoid.schemas.longform import WORDS_PER_PAGE
from prismoid.schemas.machines import FrameKind, MachineProfile, frame_labels
from prismoid.schemas.plan import StepSpec
from prismoid.schemas.qa import QACheck, QAOutcomeKind

_PRECHECK_SOURCE = Path(__file__).parent.parent / "ai" / "verification" / "qa_precheck.py"
_READER_SOURCE = _PRECHECK_SOURCE.parent / "suites" / "extract.py"


@dataclass(frozen=True, slots=True)
class QAReview:
    outcome: qa_outcome.QAOutcome
    attempt: int
    reviewer_model: str = ""
    reviewer_family: str = ""

    @property
    def must_revise(self) -> bool:
        return self.outcome.kind is QAOutcomeKind.REVISE

    @property
    def note(self) -> str:
        return self.outcome.note

    def gotcha(self) -> str:
        return qa_outcome.as_gotcha(self.outcome, self.attempt)


async def review(
    session: AsyncSession, task: Task, step: PlanStep, spec: StepSpec, produced: list[Artifact]
) -> QAReview:
    """One QA pass over the step's deliverable. NEVER raises - any unexpected failure becomes
    PASS_UNREAD with the note visible (the gate can never fail the thread)."""
    try:
        return await _review(session, task, step, spec, produced)
    except Exception:
        logfire.exception("QA pass failed for step {s}; shipping PASS_UNREAD", s=str(step.id))
        outcome = qa_outcome.outcome([], None)
        await _emit(session, task, step, outcome, step.attempt, "", "")
        return QAReview(outcome=outcome, attempt=step.attempt)


async def _review(
    session: AsyncSession, task: Task, step: PlanStep, spec: StepSpec, produced: list[Artifact]
) -> QAReview:
    from prismoid.ai.frames import ensure_ready, new_frame

    deliverable = pick_deliverable(produced)
    if deliverable is None:
        outcome = qa_outcome.outcome([], None)
        await _emit(session, task, step, outcome, step.attempt, "", "")
        return QAReview(outcome=outcome, attempt=step.attempt)

    inputs = await _gather_inputs(session, task, step, deliverable)

    # The row lock must never span the frame work (the :675 law): everything above was reads.
    await session.commit()

    acceptance = list(spec.contract.acceptance) if spec.contract else []

    # THE SIZE BAND, from the one table: the kind's own unit, the bucket's own
    # tolerance - the hardcoded `×300 words, ±0.3` pair this replaces was documents-only and
    # silently unenforced whenever target_pages was NULL.
    size = qa_size_band(task.deliverable, task.scale, task.target_pages)

    ensure_ready()
    frame = new_frame(task_id=f"qa-{step.id.hex[:8]}-{step.attempt}")
    # The reconciler's: kept while this attempt runs, or while young.
    labels = frame_labels(
        FrameKind.QA, workspace_id=task.workspace_id, task_id=task.id, step_id=step.id, attempt=step.attempt
    )
    task_id, started = task.id, None  # the frame's clock starts once it is up
    try:
        await frame.create(allow_domains=[], labels=labels)
        started = time.monotonic()
        checks, extracted, pages = await _run_precheck(frame, inputs, size)
    finally:
        try:
            await frame.destroy()
        finally:
            # The frame's warm time, once - only a frame that came up - on its own session:
            # this one was committed before the frame work, so it holds no lock on the task's row
            # the bill takes. Never raises.
            if started is not None:
                await machines.meter_throwaway(task_id, MachineProfile.BASE, int(time.monotonic() - started))

    prompt = qa_outcome.read_prompt(
        extracted=extracted, outline_md=inputs.outline_md, acceptance=acceptance, checks=checks
    )
    judged = await _read(session, task, step, deliverable, prompt, pages) if extracted.strip() else None
    outcome = qa_outcome.outcome(checks, judged.verdict if judged else None)
    if outcome.kind is QAOutcomeKind.PASS_UNREAD:
        logfire.warn(  # the receipt-less lane's regression counter - loud on purpose
            "QA gate UNREAD for task {t} step {s}: checks clean, no model read",
            t=str(task.id),
            s=str(step.id),
        )
    model, family = (judged.model, str(judged.family)) if judged else ("", "")
    await _emit(session, task, step, outcome, step.attempt, model, family)
    return QAReview(outcome=outcome, attempt=step.attempt, reviewer_model=model, reviewer_family=family)


def pick_deliverable(produced: list[Artifact]) -> Artifact | None:
    """The artifact QA reads: the step's deliverable, else its first artifact, else none."""
    return next((a for a in produced if a.role == "deliverable"), produced[0] if produced else None)


@dataclass(frozen=True, slots=True)
class _Inputs:
    """What the frame is given: the deliverable, its PDF rendition when one exists, and the
    upstream files (the outline among them)."""

    payload: bytes
    filename: str
    rendition: bytes | None
    mounts: list[tuple[str, bytes]]
    outline_md: str
    figure_ids: list[str]


async def _gather_inputs(session: AsyncSession, task: Task, step: PlanStep, deliverable: Artifact) -> _Inputs:
    """Reads only, under the caller's open transaction; commits nothing."""
    from prismoid import storage
    from prismoid.operations.execution.mounts import upstream_artifacts, upstream_filename

    payload = await storage.aget(deliverable.sha256)
    filename = str(deliverable.provenance.get("path", "deliverable.md")).rsplit("/", 1)[-1]
    # The harvest's own LibreOffice rendition, when it exists, is the PDF the visual
    # pass renders pages from - one conversion per deliverable, not two.
    rendition: bytes | None = None
    if deliverable.rendition_sha256 and deliverable.rendition_media == "application/pdf":
        try:
            rendition = await storage.aget(deliverable.rendition_sha256)
        except Exception as exc:
            logfire.info("qa: rendition unreadable ({kind}); the precheck converts", kind=type(exc).__name__)
            rendition = None  # best-effort: the precheck converts for itself
    upstream = await upstream_artifacts(session, task, step)
    mounts: list[tuple[str, bytes]] = []
    outline_md = ""
    figure_ids: list[str] = []
    for artifact in upstream:
        name = upstream_filename(artifact)
        data = await storage.aget(artifact.sha256)
        mounts.append((name, data))
        if name == "outline.md":
            outline_md = data.decode("utf-8", errors="ignore")
            figure_ids = _figure_ids(outline_md)
    return _Inputs(payload, filename, rendition, mounts, outline_md, figure_ids)


def qa_size_band(deliverable: str | None, scale: str | None, target_pages: int | None) -> dict[str, object]:
    """The precheck job's size fields for a task's deliverable kind, size bucket and target."""
    from prismoid.schemas.outputs import ArtifactType
    from prismoid.schemas.sizing import SizeBucket

    kind = ArtifactType(deliverable) if deliverable else ArtifactType.DOCUMENT
    bucket = sizing.normalize(scale) or SizeBucket.STANDARD
    size_spec = sizing.spec_for(kind, bucket)
    target_units = target_pages or 0
    return {
        "kind": kind.value,
        "unit": size_spec.unit,
        "target_units": target_units,
        "target_words": target_units * (size_spec.words_per_unit or WORDS_PER_PAGE),
        "tolerance": size_spec.qa_tolerance,
    }


async def _run_precheck(
    frame: FrameDriver, inputs: _Inputs, size: dict[str, object]
) -> tuple[list[QACheck], str, tuple[pipeline.PageImage, ...]]:
    """Writes the inputs into the frame, runs the precheck, and reads back its checks, the
    extracted text and the sampled pages. Raises when the precheck could not run."""
    await frame.write_bytes(f"/work/{inputs.filename}", inputs.payload)
    for name, data in inputs.mounts:
        await frame.write_bytes(f"/work/{name}", data)
    await ship_precheck(frame)
    if inputs.rendition is not None:
        await frame.write_bytes("/work/rendition.pdf", inputs.rendition)
    job = precheck_job(
        frame.paths.render,
        filename=inputs.filename,
        has_rendition=inputs.rendition is not None,
        has_outline=bool(inputs.outline_md),
        figure_ids=inputs.figure_ids,
        size=size,
    )
    await frame.write_bytes("/work/qa_job.json", orjson.dumps(job))
    result = await run_python(
        frame,
        "/work/qa_precheck.py",
        stdin="/work/qa_job.json",
        timeout=qa_outcome.QA_CHECK_TIMEOUT_S,
    )
    checks = precheck_checks(result)
    try:
        extracted = await frame.read_file("/work/extracted.md")
    except FileNotFoundError:
        extracted = ""
    return checks, extracted, await pipeline.sampled_pages(frame)


async def ship_precheck(frame: FrameDriver) -> None:
    """The precheck into the frame, with the one reader beside it at the relative path it loads it from."""
    await frame.write_bytes("/work/qa_precheck.py", _PRECHECK_SOURCE.read_bytes())
    await frame.write_bytes("/work/suites/extract.py", _READER_SOURCE.read_bytes())


def precheck_job(
    spell: Callable[[str], str],
    *,
    filename: str,
    has_rendition: bool,
    has_outline: bool,
    figure_ids: list[str],
    size: dict[str, object],
) -> dict[str, object]:
    """The precheck's job file: frame paths rendered for this frame, absent files as None."""
    return {
        "deliverable": spell(f"/work/{filename}"),
        "rendition": spell("/work/rendition.pdf") if has_rendition else None,
        "outline": spell("/work/outline.md") if has_outline else None,
        **size,
        "figure_ids": figure_ids,
    }


def precheck_checks(result: ExecResult) -> list[QACheck]:
    """The checks a precheck run reported on its last stdout line."""
    if result.exit_code == 0 and result.stdout.strip():
        raw = orjson.loads(result.stdout.strip().splitlines()[-1])
        return [QACheck.model_validate(c) for c in raw.get("checks", [])]
    # **A precheck that could not RUN is INFRA, not a document fact.** A non-zero
    # exit or empty stdout means the runner itself failed; recording it as a `qa:ran`
    # blocker check would floor the outcome into a forced - and billed - rebuild. QA
    # infra failure costs the GATE, never a spend: raise into `review`'s catch-all, which
    # ships PASS_UNREAD with the note visible. check-FAIL is reserved for real document
    # facts a SUCCESSFUL run returned.
    raise RuntimeError((result.stderr or "qa precheck did not run")[:400])


async def _read(
    session: AsyncSession,
    task: Task,
    step: PlanStep,
    deliverable: Artifact,
    prompt: str,
    pages: tuple[pipeline.PageImage, ...],
) -> verification.Judged | None:
    """The judge's editorial read, seated far from the maker; commits before the long call.
    None (the composer's PASS_UNREAD) when no judge answered."""
    maker = str(deliverable.provenance.get("model", ""))
    family = await verification.author_family(session, maker)
    seats = await verification.judge_seats_for(session, maker, family)
    deps = TaskDeps(
        task_id=TaskId(task.id),
        step_id=StepId(step.id),
        project_id=ProjectId(task.project_id) if task.project_id else None,
        emit_usage=usage_ops.emitter(session, own_transaction=True),
    )
    await session.commit()  # release the snapshot before the long call (the run_light_agent law)
    return await verification.ask_judge(seats, deps, prompt, pages, seconds=qa_outcome.QA_READ_TIMEOUT_S)


async def _emit(
    session: AsyncSession,
    task: Task,
    step: PlanStep,
    outcome: qa_outcome.QAOutcome,
    attempt: int,
    reviewer_model: str,
    reviewer_family: str,
) -> None:
    from prismoid.schemas.model_class import Family

    seq = await task.next_seq(session)
    await TaskEvent.append(
        session,
        QAReviewed(
            seq=seq,
            task_id=TaskId(task.id),
            step_id=StepId(step.id),
            project_id=ProjectId(task.project_id) if task.project_id else None,
            at=utcnow(),
            verdict=outcome.kind.value,
            attempt=attempt,
            findings=outcome.findings[:20],
            summary=outcome.note,
            reviewer_model=reviewer_model,
            reviewer_family=Family(reviewer_family) if reviewer_family else None,
        ),
    )


def _figure_ids(outline_md: str) -> list[str]:
    import re

    return re.findall(r"`(fig-[\w-]+)`", outline_md)
