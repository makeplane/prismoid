# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""How a step finishes: done with its receipts queued, delivered unverified or with QA notes, or
escalated with a diagnosis; and the steps waiting on it promoted.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

import logfire
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.verification.qa_outcome import notes_sentence
from prismoid.db.base import utcnow
from prismoid.db.models.artifact import Artifact
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.plan import PlanEdge as PlanEdgeRow
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.operations import inbox as inbox_ops
from prismoid.operations import verification as verification_ops
from prismoid.operations.dispatch import get_dispatcher
from prismoid.operations.execution import joins
from prismoid.operations.execution.taps import (
    narrate as _narrate,
)
from prismoid.operations.execution.taps import (
    step_event as _step_event,
)
from prismoid.schemas.events import (
    EscalationRaised,
)
from prismoid.schemas.failures import ADVICE, NARROWER, FailureKind
from prismoid.schemas.guard import (
    blocked_diagnosis,
    failed_diagnosis,
)
from prismoid.schemas.ids import ProjectId, StepId, TaskId
from prismoid.schemas.lead import Fail, FailKind, LeadEndingKind
from prismoid.schemas.outputs import ArtifactType, Outcome
from prismoid.schemas.plan import (
    StepSpec,
    StepState,
)
from prismoid.schemas.verification import Diagnosis, Verdict

# **The verify-fail retry budget - the reliability guard the user asked for.**
#
# A step that *published a deliverable* which then failed verification gets this many total attempts
# - one, plus one targeted retry with the finding in the prompt, which is exactly where the verifier
# earns its keep ("$99.7M appears in no source") - and is then **delivered honestly as `unverified`**
# rather than retried into the ground or failed outright. The artifact exists and its verdict is
# already signed and on the stream; failing the whole task over a verdict the user can already see
# - one a *weak* cross-family judge is as likely to have gotten wrong as right - throws away a good
# result and reads as "the system is flaky". This is the two symptoms the user reported (slow, and
# failing) at their shared root.
#
# It is deliberately *below* `MAX_ATTEMPTS` (3). Nothing-was-published is the one case that still
# climbs the full ladder to an escalation, because there is no deliverable to be honest about.
MAX_VERIFY_ATTEMPTS = 2


async def accept_unverified(
    session: AsyncSession, task: Task, step: PlanStep, narration: str, gotcha: str
) -> str:
    """Complete a step whose deliverable was **published but did not pass verification** - honestly.

    The counterpart to `_escalate`: same fork, opposite fact. `_escalate` is the honest end when
    *nothing* was published - there is no result, so the task fails with a diagnosis. This is the end
    when there **is** a result. Its verdict (`fail`/`unverified`) was signed and put on the stream the
    moment `_verify` ran, so the user already sees the honest amber; the only question left is whether
    the task fails *around* a downloadable artifact or delivers it with the mark showing. Honesty wants
    the mark, not the hidden truth - and with a weak cross-family judge the mark is as often the judge
    being wrong as the document being wrong. So the step goes `done`, and the deliverable stands on its
    published verdict. The narration says plainly that it shipped unverified.
    """
    step.transition(StepState.DONE)
    note = (
        f"{narration}\n\n"
        "> ⚠️ **Delivered unverified.** Verification did not pass within the retry budget; the "
        "finding is on the artifact's report and the deliverable is provided as-is."
    ).strip()
    await _narrate(session, task, step, note)
    await _step_event(session, task, step, StepState.DONE, detail=f"delivered unverified - {gotcha}")
    await promote_dependents(session, task, step)
    await session.commit()
    logfire.warn(
        "step {step_id} delivered unverified after {n} attempt(s) - the verdict is signed on the stream",
        step_id=str(step.id),
        n=step.attempt,
    )
    return note


async def accept_with_qa_notes(
    session: AsyncSession, task: Task, step: PlanStep, narration: str, review: object, *, sign: list[Artifact]
) -> str:
    """Complete an assembly whose QA findings did not clear within the rebuild budget -
    honestly (`_accept_unverified`'s doctrine, the editorial lane's words). The deliverable
    exists and is durable; a second rebuild is the budget's end, not the work's; the notes ship
    VISIBLE - in the body of the thread, on the step's record, and in the `qa.reviewed` frame
    the follow-up turn will read. Deliberately never says "unverified": that word belongs to
    the receipt lane - and `sign` still gets its receipts."""

    step.transition(StepState.DONE)
    sentence = notes_sentence(review.outcome)  # type: ignore[attr-defined]
    note = f"{narration}\n\n> {sentence}".strip()
    await _narrate(session, task, step, note)
    await _step_event(
        session, task, step, StepState.DONE, detail=f"delivered with QA notes - {sentence[:1800]}"
    )
    await promote_dependents(session, task, step)
    await session.commit()
    await enqueue_verification(sign)
    logfire.warn(
        "step {step_id} delivered with QA notes after {n} attempt(s)",
        step_id=str(step.id),
        n=step.attempt,
    )
    return note


async def enqueue_verification(produced: Sequence[Artifact], *, verify: bool = True) -> None:
    """After the step's commit, one `verify_artifact` job per deliverable artifact on its own queue;
    working-role rows and `verify=False` steps never enqueue."""

    if not verify:
        return
    for artifact in produced:
        if artifact.role != "deliverable":
            continue
        await get_dispatcher().enqueue(
            "verify_artifact", f"verify-{artifact.id}", artifact_id=str(artifact.id)
        )


# The mark a passed review leaves on a deliverable whose receipt signed unverified.
UNCHECKED_NOTE: Final = (
    "⚠️ **Not verified.** The checker could not confirm this answer; it is delivered as-is."
)


@dataclass(frozen=True)
class Receipts:
    """The in-line receipts' answer: the findings a retry is told (empty: none failed), and whether
    any receipt signed unverified."""

    findings: list[str]
    unchecked: bool


async def verify(session: AsyncSession, produced: list[Artifact]) -> Receipts:
    """Verify everything published, and return the findings - **in the words a retry can use**.

    Empty means it passed. Anything else is what the next attempt will be told, verbatim: which
    check failed, what it found, and what the cross-family judge said about it. That is the
    difference between "try again" and *"$99.7M appears in no source; the brief says $41.6M"*.
    """

    findings: list[str] = []
    unchecked = False
    for artifact in produced:
        signed = await verification_ops.verify_artifact(session, artifact)
        report = signed.report
        unchecked |= report.verdict is Verdict.UNVERIFIED
        if report.verdict is not Verdict.FAIL:
            continue

        findings.append(f"**{artifact.title}** was rejected by verification:")
        for check in report.failed_checks():
            findings.append(f"- `{check.name}` failed: {check.detail}")
            if check.actual:
                findings.append(f"  → {check.actual}")
            if check.expected:
                findings.append(f"  (expected: {check.expected})")

        judge = report.judge
        if judge is not None and judge.verdict is Verdict.FAIL:
            findings.append(f"- the verifier ({judge.family}) rejected it: {judge.rationale}")
            findings += [f"  → bad derivation: {bad}" for bad in judge.bad_derivations]
            findings += [f"  → criterion not met: {miss}" for miss in judge.criteria_missed]

    await session.commit()
    return Receipts(findings, unchecked)


def produce_diagnosis(
    made: ArtifactType | None, *, attempts: int, gotchas: list[str], failure: FailureKind | None = None
) -> Diagnosis:
    """A step that produced nothing, said in product words from its last death's `failure` kind (None: no
    attempt's death); the rungs' gotchas are the retries' brief, so they stay evidence, never the reason."""
    advice = ADVICE[failure or FailureKind.MODEL_FAILED]
    if advice.final:
        what = "reach the site"
    else:
        what = f"make the {made.value.replace('_', ' ')}" if made else "finish this part of the task"
    tries = "1 try" if attempts == 1 else f"{attempts} tries"
    return Diagnosis(
        what_failed=f"Prismoid could not {what}",
        why=f"{advice.said} ({tries}).",
        attempts=attempts,
        evidence=gotchas,
        suggested_next=list(advice.suggestions) if failure is not None else [NARROWER],
    )


async def escalate(
    session: AsyncSession,
    task: Task,
    step: PlanStep,
    spec: StepSpec,
    gotchas: list[str],
    *,
    ended: Outcome | None = None,
    failure: FailureKind | None = None,
    stopped: Fail | None = None,
) -> None:
    """Stop and say why, in a form a person can act on: what failed, the evidence of every rung, and what
    to try next. `stopped`: an unattended Lead ended in a typed failure."""

    step.transition(StepState.ESCALATED)

    if stopped is not None:
        diagnosis = lead_diagnosis(stopped, attempts=step.attempt)
    elif ended is not None and ended.status == "blocked":
        # A guarded step that stopped at a wall says which wall, and what
        # a person does about it - it did not fail to produce, it was stopped.
        diagnosis = blocked_diagnosis(ended, attempts=step.attempt, evidence=gotchas)
    elif ended is not None and ended.status == "failed":
        diagnosis = failed_diagnosis(ended, attempts=step.attempt, evidence=gotchas)
    else:
        made = spec.contract.output.type if spec.contract and spec.contract.output else None
        diagnosis = produce_diagnosis(made, attempts=step.attempt, gotchas=gotchas, failure=failure)

    seq = await task.next_seq(session)
    await TaskEvent.append(
        session,
        EscalationRaised(
            seq=seq,
            task_id=TaskId(task.id),
            step_id=StepId(step.id),
            project_id=ProjectId(task.project_id) if task.project_id else None,
            at=utcnow(),
            diagnosis=diagnosis,
        ),
    )
    await _step_event(
        session,
        task,
        step,
        StepState.ESCALATED,
        detail=diagnosis.why,
        outcome=ended.status if ended is not None else None,
        blocked_by=ended.blocked_by if ended is not None else None,
        ending=LeadEndingKind.FAIL if stopped is not None else None,
    )
    await inbox_ops.escalated(session, task, diagnosis)


# What each typed failure of an unattended run asks of its owner.
_OWNER_NEXT: Final[dict[FailKind, str]] = {
    FailKind.NEEDS_OWNER: "Do the part only you can (sign in, connect the service, or settle the choice), "
    "then run it again.",
    FailKind.UNAVAILABLE: "Check the source or service is up and reachable, then run it again.",
    FailKind.REFUSED: "Change the instruction so it asks for something the run may do.",
}


def lead_diagnosis(stopped: Fail, *, attempts: int) -> Diagnosis:
    """An unattended run's typed failure, said to its owner: what stopped it, and what they do next."""
    return Diagnosis(
        what_failed=f"The run stopped on its own ({stopped.kind.value.replace('_', ' ')})",
        why=stopped.reason,
        attempts=attempts,
        suggested_next=[_OWNER_NEXT[stopped.kind]],
    )


async def promote_dependents(session: AsyncSession, task: Task, done: PlanStep) -> None:
    """A step finished. Anything that was waiting **only** on it can now run.

    Without this a multi-step plan is not a plan, it is a list: the first step succeeds, the second
    is still `pending`, and the executor refuses it with *"step … is pending; it cannot be
    executed"* - which is the machine correctly declining to run work whose inputs do not exist yet,
    and reporting it as a task failure because nobody ever told it the inputs had arrived.

    Only when **every** upstream step is done. A step with two dependencies that starts when the
    first one lands is a step reading half its sources, and it will not notice.

    ────────────────────────────────────────────────────────────────────────────────────────────
    **The caller must already hold the task's row lock, and it does.**

    Under a fan-out two steps finish in two transactions at once. If `C` depends on both `A`
    and `B`, and each of their transactions reads the other as *not yet done*, then **neither
    promotes C** - C stays `pending`, the frontier comes back empty, and the task **delivers without
    it**. A silent partial delivery is the worst outcome this system can produce: it looks exactly
    like success.

    What prevents it is that every path into this function has already called `next_seq`, which is an
    `UPDATE … RETURNING` and therefore holds a **row lock on the task until COMMIT**. The second
    finisher blocks there, and by the time it reads, the first one's `done` is committed and visible
    (Postgres READ COMMITTED). The lock is a real mutex over step completion for a task.

    That is load-bearing and it is not obvious, so: **do not call this before `_step_event`.** And
    because relying on an invariant nobody checks is how the invariant stops holding, `deliver`
    refuses to deliver a task with an unfinished step.
    """
    edges = await PlanEdgeRow.for_task(session, task.id)
    steps = {s.id: s for s in await PlanStep.for_task(session, task.id)}

    waiting_on: dict[uuid.UUID, set[uuid.UUID]] = {}
    for edge in edges:
        waiting_on.setdefault(edge.to_step_id, set()).add(edge.from_step_id)

    for step_id, upstream in waiting_on.items():
        step = steps.get(step_id)
        if step is None or step.step_state is not StepState.PENDING:
            continue
        spec = StepSpec.model_validate(step.spec or {})
        states = [steps[u].step_state for u in upstream if u in steps]
        primary_state = None
        if spec.primary_input is not None:
            primary = steps.get(uuid.UUID(str(spec.primary_input)))
            primary_state = primary.step_state if primary is not None else StepState.SKIPPED

        # **The join law is `execution/joins.decide` - pure, total, exhaustively tested
        # (ADR-028).** This loop, holding the task row lock, only carries out the verdict:
        # PROMOTE flips READY; SKIP settles a step whose primary died (a harmonize pass for
        # a lost section), so its own dependents' joins see one more settled, not-done
        # producer; WAIT reads again at the next completion.
        verdict = joins.decide(spec, states, primary_state)
        if verdict is joins.JoinDecision.SKIP:
            step.transition(StepState.SKIPPED)
            await _step_event(
                session,
                task,
                step,
                StepState.SKIPPED,
                detail="its primary upstream was lost - nothing for this step to do",
            )
        elif verdict is joins.JoinDecision.PROMOTE:
            step.transition(StepState.READY)
            await _step_event(session, task, step, StepState.READY)
            logfire.info("step {step} is ready - {done} finished", step=str(step.id), done=str(done.id))
