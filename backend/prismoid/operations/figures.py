# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Figure pre-generation (ADR-030) - every image the outline promises, generated ONCE,
in parallel, the moment the outline lands - concurrent with the section wave.

The outline is the index: `OutlineResult` enumerates every `FigureSpec` (stable id, what it
depicts, caption hint) before a single section is written, and no image depends on section
prose. Generating them inside assembly - the old shape - put 10–30 s per image on the END of
the critical path and, worse, re-bought every image on every assembly retry (a fresh sandbox
has no yesterday): the first live deck generated its five figures THREE times, and the extra
agent round-trips helped push it over the request-budget cliff.

Here: one lightweight frame (the QA composer's pattern - images still enter through the only
door, `frame.publish` → manifest → harvest), all figures fired concurrently under the
gateway's per-provider limiter, each published as a WORKING artifact attributed to the
turn's own outline step - the NEWEST done outline, because a follow-up keeps earlier turns'
done steps and ordinal-first would pin the images to a step no current edge mounts (review
M1). The step's `outline → assembly` edge then mounts them into every assembly
attempt for free - including a QA rebuild, which is why lateness is never a reason not to
publish. A lost figure costs its placeholder caption (the standing degrade law); a lost job
costs ALL the placeholders and never the task; a straggler is covered by assembly's
`generate_image` fallback grant.

The money laws:
- every successful wire call is metered THROUGH its candidate's own `units_emitter` the
  moment it returns - binding the emitter is not emitting (the review's critical finding);
- the job spends only while its task is RUNNING on its own turn - checked at entry and again
  before the second sweep buys anything; bought images still publish while the task is
  merely parked (waiting/paused), never once it is cancelled, failed, or delivered;
- re-entry is idempotent: a figure whose PNG already sits on the outline step (a recovered
  worker, a duplicate wake) is never bought twice;
- the enqueue itself is HELD when the expansion cost checkpoint is about to park the task -
  `gates.resolve`'s accept releases it - so a declined checkpoint never buys a pixel
  (`operations/execution` and `operations/gates` are the two call sites, both post-commit).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from typing import Final

import logfire
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.gateway.meter import MeterContext, units_emitter
from prismoid.ai.gateway.providers import api_key_for
from prismoid.ai.gateway.resolve import chain_for
from prismoid.ai.gateway.throttle import throttled
from prismoid.ai.tools.providers.images import IMAGE_TIMEOUT_S, ImageCandidate, Size, wire_for
from prismoid.db.models.artifact import Artifact
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.schemas.ids import ProjectId, StepId, TaskId
from prismoid.schemas.longform import OutlineResult
from prismoid.schemas.machines import FrameKind, MachineProfile, Segment, frame_labels
from prismoid.schemas.model_class import ModelClass
from prismoid.schemas.outputs import ArtifactType
from prismoid.schemas.plan import StepSpec, TaskStatus

FIGURE_SIZE: Size = "landscape"  # figures sit in prose columns and slides - wide
SWEEP_BREATH_S: Final = 30.0  # the second sweep waits out a provider's per-minute rate window

# States a bought image may still be PUBLISHED under: a parked task resumes and its assembly
# will mount the images; a dead or delivered task will not, so pinning rows to it is litter.
_PUBLISHABLE: Final = frozenset({TaskStatus.RUNNING, TaskStatus.WAITING, TaskStatus.PAUSED})


async def enqueue_pregeneration(task_id: uuid.UUID, turn: int) -> None:
    """The one enqueue - deterministic id, always after a commit. DBOS id-dedupe
    makes the second caller (the checkpoint-accept release) harmless when both fire."""
    from prismoid.operations.dispatch import get_dispatcher

    await get_dispatcher().enqueue(
        "pregenerate_figures", f"figures-{task_id}-t{turn}", task_id=str(task_id), turn=turn
    )


def _prompt(outline: OutlineResult, spec_text: str, caption: str) -> str:
    """Deterministic composition - the spec IS the brief; the bible's first line lends voice."""
    style = outline.continuity_bible.splitlines()[0].strip() if outline.continuity_bible else ""
    return (
        f"{spec_text}. {('Caption: ' + caption + '.') if caption else ''} "
        f"Clean editorial illustration suitable for a professional document titled "
        f'"{outline.title}". {style} No text overlays, no watermarks.'
    )


def _stale_reason(task: Task | None, turn: int) -> str | None:
    """Why this job must not SPEND right now: the stale-writer law plus the review's money
    guard - the next turn owns its own figures, and a parked, killed or settled task buys
    nothing on its behalf."""
    if task is None:
        return "the task is gone"
    if task.turn != turn:
        return "the turn moved on"
    if task.state is not TaskStatus.RUNNING:
        return f"the task is {task.status}, not running"
    return None


async def generate_batch(
    wanted: Sequence[tuple[str, str]],
    candidates: Sequence[ImageCandidate],
    *,
    breath_s: float = SWEEP_BREATH_S,
    sweep_allowed: Callable[[], Awaitable[bool]] | None = None,
) -> list[tuple[str, bytes]]:
    """The fan-out, pure of persistence (unit-testable with fake wires): every figure
    concurrently, each falling down the candidate chain; then ONE bounded second sweep for
    exactly the stragglers, after a breath (24 concurrent requests trip a
    provider's images-per-minute quota - spread out, a retry clears the window). What still
    fails costs the assembly fallback, as ever.

    Rule 8 lives HERE: each successful wire call emits its own units before the image is
    accepted - the per-candidate emitter opens its own short transaction, so the concurrent
    gather cannot trip over a shared session. A hung provider costs one `IMAGE_TIMEOUT_S`,
    never the batch. `sweep_allowed` is the money guard's re-check seam: the second sweep
    buys nothing for a task that parked or died during the first."""

    async def one(fig_id: str, prompt: str) -> tuple[str, bytes] | None:
        for candidate in candidates:
            wire = wire_for(candidate.protocol)
            if wire is None:
                continue
            try:
                async with throttled(candidate.provider):
                    async with asyncio.timeout(IMAGE_TIMEOUT_S):
                        images = await wire(candidate, prompt, FIGURE_SIZE, 1)
                if images:
                    await candidate.emit_units({"image": len(images), "request": 1})
                    return fig_id, images[0]
            except Exception as failed:
                logfire.warn(
                    "figures: {ref} failed for {fig} ({e}) - trying the next candidate",
                    ref=candidate.ref,
                    fig=fig_id,
                    e=f"{type(failed).__name__}",
                )
        return None

    results = await asyncio.gather(*(one(fid, prompt) for fid, prompt in wanted))
    landed = [r for r in results if r is not None]
    missing = [(fid, prompt) for fid, prompt in wanted if fid not in {fid for fid, _ in landed}]
    if missing:
        await asyncio.sleep(breath_s)
        if sweep_allowed is None or await sweep_allowed():
            retry = await asyncio.gather(*(one(fid, prompt) for fid, prompt in missing))
            landed += [r for r in retry if r is not None]
    return landed


async def pregenerate(session: AsyncSession, task_id: uuid.UUID, turn: int) -> int:
    """Generate every outline-promised figure concurrently; publish each as a working
    artifact on the turn's own outline step. Returns how many landed. NEVER raises past
    itself - a figure job's death costs placeholders, not the task."""
    from prismoid.operations import models as model_ops
    from prismoid.operations import usage as usage_ops

    task = await session.get(Task, task_id)
    reason = _stale_reason(task, turn)
    if task is None or reason is not None:
        logfire.info("figures: skipped - {reason}", reason=reason or "the task is gone")
        return 0
    steps = await PlanStep.for_task(session, task_id)
    outline_step = max(
        (s for s in steps if StepSpec.model_validate(s.spec).lane_role == "outline" and s.state == "done"),
        key=lambda s: s.ordinal,
        default=None,
    )
    if outline_step is None:
        return 0
    from prismoid.db.models.plan import PlanEdit as PlanEditRow

    row = await PlanEditRow.latest_expand(session, task_id)
    if row is None:
        return 0
    outline = OutlineResult.model_validate(row.payload)
    figures = [(s, f) for s in outline.sections for f in s.figures]
    if not figures:
        return 0

    # Idempotent re-entry (review): a recovered worker or a duplicate wake re-buys nothing -
    # what already landed on the outline step stays bought once, and only the rest is wanted.
    already = await Artifact.titles_for_step(
        session, outline_step.id, role="working", artifact_type=ArtifactType.IMAGE.value
    )
    wanted = [(f.id, _prompt(outline, f.spec, f.caption_hint)) for _s, f in figures if f.id not in already]
    if not wanted:
        return 0

    emit = usage_ops.emitter(session, own_transaction=True)
    ctx = MeterContext(
        task_id=TaskId(task.id),
        step_id=StepId(outline_step.id),
        project_id=ProjectId(task.project_id) if task.project_id else None,
    )
    catalog, order = await model_ops.routed_catalog(session)
    await session.commit()  # no row lock across provider calls (the run_light_agent law)

    candidates = [
        ImageCandidate(
            provider=m.provider,
            model_id=m.model_id,
            protocol=str(m.protocol),
            base_url=m.base_url,
            api_key=api_key_for(m.provider),
            emit_units=units_emitter(m, emit, ctx),
        )
        for m in chain_for(ModelClass.IMAGE_GEN, catalog, order=order.get(ModelClass.IMAGE_GEN))
    ]
    if not candidates:
        logfire.warn("figures: no image candidates enabled; placeholders will stand")
        return 0

    async def sweep_allowed() -> bool:
        probe = await session.get(Task, task_id)
        if probe is not None:
            await session.refresh(probe)  # minutes may have passed; read the row, not the cache
        why = _stale_reason(probe, turn)
        if why is not None:
            logfire.warn("figures: second sweep skipped - {why}", why=why)
            return False
        return True

    landed = await generate_batch(wanted, candidates, sweep_allowed=sweep_allowed)
    if not landed:
        return 0

    # The publish guard (review): bought images still mount for a merely-parked task, but a
    # cancelled, failed or delivered one gets nothing new pinned to its record.
    await session.refresh(task)
    if task.turn != turn or task.state not in _PUBLISHABLE:
        logfire.warn(
            "figures: {n} generated but not published - task is {s} (turn {t}→{now})",
            n=len(landed),
            s=task.status,
            t=turn,
            now=task.turn,
        )
        return 0

    produced = await _publish_batch(session, task, outline_step, landed, turn)
    logfire.info(
        "figures: {n}/{total} pre-generated for task {t}", n=len(produced), total=len(figures), t=str(task_id)
    )
    return len(produced)


async def _publish_batch(
    session: AsyncSession, task: Task, outline_step: PlanStep, landed: Sequence[tuple[str, bytes]], turn: int
) -> list[Artifact]:
    """One frame for the whole batch: the only door still stands - write, publish, harvest. The
    frame is labelled for the reconciler: a leftover goes once it is older than a step
    can run.

    Its warm time - from when the frame is up - bills once, after the destroy, on THIS
    session: a harvest that failed may hold the task's row here uncommitted, and a bill on its own
    session would wait on that lock forever. So it commits with the batch, and a failed batch's
    goes with the job's rollback. A frame that never came up bills nothing."""
    from prismoid.ai.frames import ensure_ready, new_frame
    from prismoid.operations import machines
    from prismoid.operations.execution.harvest import harvest as _harvest

    ensure_ready()
    frame = new_frame(task_id=f"figures-{task.id.hex[:8]}-t{turn}")
    task_id, started = task.id, None
    try:
        await frame.create(
            allow_domains=[],
            labels=frame_labels(FrameKind.FIGURES, workspace_id=task.workspace_id, task_id=task.id),
        )
        started = time.monotonic()
        for fig_id, data in landed:
            path = f"/work/{fig_id}.png"
            await frame.write_bytes(path, data)
            await frame.publish(path, ArtifactType.IMAGE.value, fig_id, sources=[])
        produced = await _harvest(session, task, outline_step, frame, role="working")
        await session.commit()
    finally:
        await frame.destroy()
        if started is not None:
            await machines.bill(
                session, Segment(task_id, MachineProfile.BASE, int(time.monotonic() - started))
            )
    await session.commit()  # the bill
    return produced
