# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The published lane: the ANSWER is primary; verification is its receipt.

A standard step that published is done now, and the `verify_artifact` job signs the receipt after
delivery. Two lanes keep one in-line review, because their mode asked for it: the FLEET (a seat's answer is
verified, then ships with the mark) and a QA step (the judge's editorial review of the long-form
assembly, then it ships with the notes showing). Findings become the retry's brief either way, and
the review is fenced again right before done: a steer during it goes round.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import logfire

from prismoid.operations import early_boot, gates
from prismoid.operations import figures as figures_ops
from prismoid.operations import qa as qa_ops
from prismoid.operations import swarm as swarm_ops
from prismoid.operations.execution import finish, steers
from prismoid.operations.execution.handlers.state import AGAIN, Ladder, Ran, Round, go_round
from prismoid.operations.execution.taps import narrate, step_event
from prismoid.operations.turn.commit import Committing
from prismoid.schemas.lead import Deliver
from prismoid.schemas.longform import OutlineResult
from prismoid.schemas.outputs import Outcome
from prismoid.schemas.plan import StepState, TaskMode

# A retry's brief claims a reviewed draft only on the two lanes that review in line (fleet, QA).
_FLEET_PREFIX = (
    "- A verifier re-derived the figures in the file you published and found this"
    " (revise the reviewed draft; do not start from scratch):"
)
_QA_PREFIX = (
    "- The editorial review of the file you published found this"
    " (revise the reviewed draft; do not start from scratch):"
)


@dataclass(frozen=True)
class _Reviewed:
    """One in-line review's answer: the findings a retry is told (empty: it passed), how the draft
    ships past the budget, and a note for the narration on a pass."""

    findings: str
    accept: Callable[[], Awaitable[str]]
    budget: int
    note: str = ""


async def deliver(ladder: Ladder, ran: Ran, ended: Outcome | None) -> str | Round:
    """The step done and its dependents promoted (its narration the final word, or nothing for the
    summary when it only handed the turn to work), or `AGAIN` when a review sends it round. Commits
    the done; enqueues the receipts and the figures after it."""
    fleet = TaskMode(ladder.task.mode) in (TaskMode.FLEET, TaskMode.COUNCIL)
    if ran.produced and fleet:
        said = await _review(ladder, ran, _FLEET_PREFIX, None, _verified)
    elif ran.produced and ladder.spec.qa:
        said = await _review(
            ladder, ran, _QA_PREFIX, "QA review - reading the assembled deliverable", _edited
        )
    else:
        # THE STANDARD LANE: published means done - running → done, the user's answer is on its
        # way NOW, and the receipt job signs the verdict onto the same stream after delivery.
        # Nothing between the artifact and the human (fence 1 ran above; a steer after it is
        # fence 2's, at delivery).
        ladder.step.transition(StepState.DONE)
        said = None
    if said is not None:
        return said
    await _done(ladder, ran, ended, fleet=fleet)
    return "" if ran.interim else ran.narration


async def _review(
    ladder: Ladder,
    ran: Ran,
    prefix: str,
    detail: str | None,
    read: Callable[[Ladder, Ran], Awaitable[_Reviewed]],
) -> str | Round | None:
    """The in-line review for the lanes whose mode asked for it: the judge's findings brief a bounded retry,
    then the draft ships with its mark. None means it passed and the step is done."""
    step = ladder.step
    if step.step_state is not StepState.VERIFYING:  # already there on a review resume
        step.transition(StepState.VERIFYING)
    if detail is None:
        await step_event(ladder.session, ladder.task, step, StepState.VERIFYING)
    else:
        await step_event(ladder.session, ladder.task, step, StepState.VERIFYING, detail=detail)
    await ladder.session.commit()
    reviewed = await read(ladder, ran)
    if reviewed.findings:
        ladder.gotchas.append(f"{prefix}\n{reviewed.findings}")
        if step.model_attempts() >= reviewed.budget:
            if await _refenced(ladder, ran):
                return AGAIN
            return await reviewed.accept()
        logfire.warn(
            "step {step_id} failed its review (attempt {n}) - retrying with the findings",
            step_id=str(step.id),
            n=step.attempt,
        )
        # A retry is a fresh attempt; the person sees the findings.
        return await go_round(ladder, reviewed.findings[:2000])
    if await _refenced(ladder, ran):
        return AGAIN
    if reviewed.note:
        ran.narration = f"{ran.narration}\n\n> {reviewed.note}" if ran.narration else f"> {reviewed.note}"
    step.transition(StepState.DONE)  # verifying → done: the review passed
    return None


async def _refenced(ladder: Ladder, ran: Ran) -> bool:
    """A steer during the review: fence 1 again, right before done."""
    return await steers.fence(ladder.session, ladder.task, ladder.step, ran.produced, ladder.gotchas)


async def _verified(ladder: Ladder, ran: Ran) -> _Reviewed:
    """The fleet's read: each seat's answer verified in line, the receipt's checks and judge; past the
    budget it ships with the amber mark (a lost seat costs the seat, never the fleet)."""
    session, task, step = ladder.session, ladder.task, ladder.step
    await steers.reread_epoch(session, step)  # the receipt checks the steers the run took
    receipts = await finish.verify(session, ran.produced)
    findings = "\n".join(receipts.findings)
    return _Reviewed(
        findings,
        lambda: finish.accept_unverified(session, task, step, ran.narration, findings),
        finish.MAX_VERIFY_ATTEMPTS,
        finish.UNCHECKED_NOTE if receipts.unchecked else "",  # a seat no checker confirmed says so
    )


async def _edited(ladder: Ladder, ran: Ran) -> _Reviewed:
    """The long-form lane's read: the judge's editorial review of the assembled deliverable, against
    the contract as steered; past the budget it ships with the notes showing."""
    session, task, step, spec = ladder.session, ladder.task, ladder.step, ladder.spec
    review = await qa_ops.review(
        session, task, step, await steers.steered(session, task, step, spec), ran.produced
    )
    sign = ran.produced if spec.verify else []
    return _Reviewed(
        review.gotcha() if review.must_revise else "",
        lambda: finish.accept_with_qa_notes(session, task, step, ran.narration, review, sign=sign),
        qa_ops.qa_outcome.MAX_QA_ATTEMPTS,
        "" if review.must_revise else review.note,  # a note rides a pass only
    )


async def _done(ladder: Ladder, ran: Ran, ended: Outcome | None, *, fleet: bool) -> None:
    """A done step on the record, its dependents promoted, committed; then its receipts and its
    figures enqueued."""
    session, task, step, spec = ladder.session, ladder.task, ladder.step, ladder.spec
    if ran.narration:
        await narrate(session, task, step, ran.narration)
    lead = ran.outline if isinstance(ran.outline, Committing) else None
    status = ended.status if ended is not None else None
    findings = lead.findings if isinstance(lead, Deliver) else None
    await step_event(
        session,
        task,
        step,
        StepState.DONE,
        outcome=status,
        ending=lead.ending if lead else None,
        findings=findings,
    )
    # THE ITERATION LAW: a settled seat asks the tally whether its wave is whole and short - AFTER
    # its own DONE (the wave-settled read must count this seat) and BEFORE promotion, so a
    # re-scatter wave lands first and synthesis structurally waits for the new round. Best-effort
    # by contract: a failed tally costs the round, never the task.
    if spec.lane_role == "gather":
        await swarm_ops.after_gather(session, task, step)
    await finish.promote_dependents(session, task, step)
    await session.commit()
    # After the commit: the receipts. Standard lane only - the fleet already verified in-line and
    # recorded its verdicts above; a `verify=False` spec skips inside the enqueue.
    if ran.produced and not fleet:
        await finish.enqueue_verification(ran.produced, verify=spec.verify)
    if spec.lane_role == "express":  # the Lead's plan may hold computer work: its Machine starts now
        await early_boot.for_plan(session, task.id)
    outline = ran.outline
    if (
        spec.lane_role == "outline"
        and isinstance(outline, OutlineResult)
        and any(s.figures for s in outline.sections)
    ):
        await _figures(ladder)


async def _figures(ladder: Ladder) -> None:
    """THE FIGURE FLEET, after the commit like every enqueue: the images generate in parallel WHILE
    the section wave writes, land as working artifacts on this outline step, and mount into every
    assembly attempt via the outline → assembly edge - never re-bought by a retry again. But never
    BEFORE consent: when the expansion cost checkpoint is about to park this task, the gate's
    accept releases the fleet instead - a declined checkpoint never buys a pixel."""
    task = ladder.task
    if await gates.confirmation_ahead(ladder.session, task):
        logfire.info("figures: held for the cost checkpoint on task {t}", t=str(task.id))
    else:
        await figures_ops.enqueue_pregeneration(task.id, task.turn)
