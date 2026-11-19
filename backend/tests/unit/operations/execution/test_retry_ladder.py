# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The retry ladder's rules - the parts that must not depend on a model's mood.

The live run is the evidence that it *works*. These are the invariants that must keep
working: the state machine's shape, the fact that a retry is a **fresh attempt**, and - the one that
actually matters - that the prompt a retry gets is **targeted**, not a shrug.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest

from prismoid.ai.context.brief import prompt_for
from prismoid.ai.context.retry import continue_prompt
from prismoid.db.models.plan import MAX_ATTEMPTS, PlanStep
from prismoid.operations import qa as qa_ops
from prismoid.operations.execution import finish, steers
from prismoid.operations.execution.finish import Receipts
from prismoid.operations.execution.handlers import produce, published
from prismoid.operations.execution.handlers import state as ladder_state
from prismoid.operations.execution.handlers.state import Ladder, Ran
from prismoid.operations.execution.record import owe_run, owed_run, with_record
from prismoid.schemas.contract import Contract
from prismoid.schemas.failures import FailureKind
from prismoid.schemas.ids import AgentName
from prismoid.schemas.outputs import ArtifactType, OutputRef
from prismoid.schemas.park import OwedRun
from prismoid.schemas.plan import StepKind, StepSpec, StepState, TaskMode
from prismoid.schemas.steering import SteerNote


def a_spec() -> StepSpec:
    return StepSpec(
        kind=StepKind.STEP,
        agent=AgentName("writer"),
        title="a memo",
        contract=Contract(
            goal="Write a memo on Q3.",
            acceptance=["every figure comes from the brief"],
            inputs=["/task/inputs/brief.md"],
            output=OutputRef(type=ArtifactType.DOCUMENT, title="memo"),
        ),
    )


# ── the shape of the ladder ────────────────────────────────────────────────────────────────────


def test_a_retry_re_enters_at_ready_because_it_is_a_fresh_attempt() -> None:
    """`verifying → retrying → ready → running`. **Never `retrying → running`.**

    A retry is not a resumed attempt, it is a new one: a new sandbox, new model calls, a new bill.
    Routing it through `ready` is what makes the attempt counter mean something and what makes the
    frame the unit of recovery.
    """
    step = PlanStep(state=StepState.VERIFYING.value)
    step.transition(StepState.RETRYING)
    step.transition(StepState.READY)
    step.transition(StepState.RUNNING)
    assert step.step_state is StepState.RUNNING


def test_a_recovery_rung_never_costs_a_model_attempt() -> None:
    """The retry caps read `model_attempts() = attempt − infra_attempts`, so a
    re-bought sandbox on recovery - a worker restart, a reaped parked frame, a wall-clock stop,
    a kill-during-QA that re-buys the assembly - bumps BOTH counters and nets to zero model rungs.

    This is the arithmetic the execution recovery paths maintain (findings: an infra stop bumps
    infra in the denied arm; a VERIFYING re-entry that re-buys a sandbox bumps infra too). If a
    cap ever read `attempt` directly again, a restart would silently spend the user's ladder - the
    live compounding that ate the QA rebuild budget before QA ever ran.
    """
    step = PlanStep(attempt=2, infra_attempts=0)
    before = step.model_attempts()
    step.attempt += 1  # a fresh sandbox (frame-name uniqueness)…
    step.infra_attempts += 1  # …cancelled out, because the death was infra, not the model
    assert step.model_attempts() == before, "a recovered attempt is not a model attempt"


def test_a_verifying_reentry_that_re_buys_nothing_moves_no_counter() -> None:
    """A VERIFYING re-entry whose produce is already durable resumes AT the
    review - it re-buys no sandbox, so it bumps NEITHER `attempt` NOR `infra_attempts`. Bumping
    infra alone (the pre-fix bug) would DECREMENT `model_attempts()` below its true value and hand
    out an extra billed rebuild past MAX_QA_ATTEMPTS."""
    step = PlanStep(attempt=2, infra_attempts=1)
    before = step.model_attempts()
    # resume-at-review feeds the durable artifacts straight to the outcome arm: no counters move.
    assert step.model_attempts() == before == 1


def test_published_means_done_and_the_receipt_follows() -> None:
    """`running → done` IS a transition: the standard lane completes the moment the
    artifact exists - the user's answer is primary - and the `verify_artifact` job signs the
    verdict onto the stream after delivery. `verifying` survives as the fleet's in-line lane."""
    step = PlanStep(state=StepState.RUNNING.value)
    step.transition(StepState.DONE)
    assert step.step_state is StepState.DONE

    fleet_lane = PlanStep(state=StepState.RUNNING.value)
    fleet_lane.transition(StepState.VERIFYING)
    fleet_lane.transition(StepState.DONE)
    assert fleet_lane.step_state is StepState.DONE


def test_three_attempts_then_escalate() -> None:
    assert MAX_ATTEMPTS == 3
    step = PlanStep(state=StepState.VERIFYING.value)
    step.transition(StepState.ESCALATED)
    # …and a human who fixes the input can send it round again.
    step.transition(StepState.READY)


# ── the prompt: targeted, not a shrug ──────────────────────────────────────────────────────────


def test_the_first_attempt_gets_the_contract_and_nothing_else() -> None:
    prompt = prompt_for(a_spec())
    assert "Write a memo on Q3." in prompt
    assert "/task/inputs/brief.md" in prompt
    assert "REJECTED" not in prompt


def test_a_retry_is_told_exactly_what_was_wrong() -> None:
    """**This is the whole micro.**

    "That failed, try again" produces the same document with different adjectives. The model gets
    the check that failed, the figure that was ungrounded, and the judge's reasoning - verbatim. It
    is not being asked to guess what went wrong. It is being told, and the fix is usually one
    sentence.
    """
    gotcha = (
        "**Q3 Memo** was rejected by verification:\n"
        "- `numbers:grounded` failed: 1 ungrounded\n"
        "  → in no source: 99.7M\n"
        "- the verifier (qwen) rejected it: the memo states a revenue figure that appears nowhere."
    )
    prompt = prompt_for(a_spec(), [gotcha])

    assert "did not finish" in prompt
    assert "attempt 2" in prompt
    assert "99.7M" in prompt  # the *figure*, not a category
    assert "appears nowhere" in prompt  # the judge's own words
    assert "different family" not in prompt  # a review is named only by the lane that ran one

    # The contract is still there. A retry that forgets the goal is a retry that answers a different
    # question very carefully.
    assert "Write a memo on Q3." in prompt
    assert "/task/inputs/brief.md" in prompt


def test_the_ladder_accumulates_so_attempt_three_can_see_attempt_one() -> None:
    prompt = prompt_for(a_spec(), ["first was wrong", "second was wrong too"])
    assert "attempt 3" in prompt
    assert "first was wrong" in prompt
    assert "second was wrong too" in prompt


# The diagnosis's advice per failure kind: tests/unit/operations/execution/test_failure_kinds.py.


def test_a_carried_retry_is_told_why_it_stopped_and_not_to_redo() -> None:
    """Agent loop spec 3.4: the next user turn of a carried conversation. Never the contract
    again, never "publish it again" - the model already holds the brief and what it did."""
    text = continue_prompt("the run hit its request ceiling;\n  continue from where you stopped")
    assert text.startswith("Your previous attempt stopped: the run hit its request ceiling. Continue from")
    assert text.lower().count("continue from where you stopped") == 1, "said once, never twice"
    assert "do not redo actions that already took effect" in text
    assert "Check the page or files first if unsure." in text
    assert "publish" not in text
    assert len(continue_prompt("x" * 5000)) < 800, "a runaway reason is clipped, not pasted"


def test_a_resumed_device_attempt_is_told_the_command_that_was_cut_off() -> None:
    """The attempt a person resumed after a crash hears the cut-off command
    and to look first. Without one the prompt is exactly what it always was."""
    told = prompt_for(a_spec(), interruption="git push origin main")
    assert told == prompt_for(a_spec()) + (
        "\n\nYour last command was cut off: `git push origin main`. Check the folder (git status, the "
        "files you touched) before you act."
    )


def test_a_command_that_ran_on_is_told_as_still_running() -> None:
    """B6 round 1 (M7): only the worker went away, so the command may have finished - never "cut off"."""
    told = prompt_for(a_spec(), interruption="npm test", still_running=True)
    assert told.endswith(
        "\n\nYour last command was still running when Prismoid recovered: `npm test`. Check whether it "
        "finished (git status, the files it touched) before you act."
    )
    assert "cut off" not in told


def test_a_command_that_finished_unseen_is_told_with_its_exit_code() -> None:
    """It finished with its worker gone: the spec's "whether it finished", never "cut off"."""
    told = prompt_for(a_spec(), interruption="git push", exit_code=1)
    assert told.endswith(
        "\n\nYour last command finished while Prismoid was away, and you never saw its result (exit 1): "
        "`git push`. Check what it did (git status, the files it touched) before you act, and do not "
        "run it again to find out."
    )
    assert "cut off" not in told


def test_a_guarded_fresh_retry_never_hears_publish_it_again() -> None:
    """A guarded step whose conversation could not be carried starts from the contract, told what
    stopped it - and NOT the document producer's "a verifier read what you published" block."""
    gotcha = "- **nothing was published.** the provider connection failed mid-run"
    text = prompt_for(a_spec(), [gotcha], guarded=True)
    assert "## Your previous attempt stopped (this is attempt 2)" in text
    assert gotcha in text
    assert "publish it again" not in text and "REJECTED" not in text
    assert "do not redo actions that already took effect" in text
    assert "publish it again" in prompt_for(a_spec(), [gotcha]), "unguarded steps keep today's block"


# ── who is named as the reviewer: only the lane that ran one ──────────────────────────────────


def _running_step() -> PlanStep:
    step = PlanStep(id=uuid.uuid4(), task_id=uuid.uuid4(), ordinal=0, kind="step", title="a memo", spec={})
    step.state = StepState.RUNNING.value
    step.attempt = 1
    step.infra_failures = 0
    step.infra_attempts = 0
    return step


class _Session:
    async def refresh(self, obj: Any, attribute_names: Any = None) -> None:
        return None

    async def commit(self) -> None:
        return None


def _ladder(spec: StepSpec, *, mode: TaskMode = TaskMode.STANDARD) -> Ladder:
    task = SimpleNamespace(mode=mode.value, status="running", is_terminal=lambda: False)
    return cast(
        "Ladder",
        SimpleNamespace(
            session=_Session(),
            task=task,
            step=_running_step(),
            spec=spec,
            contract=spec.contract,
            gotchas=[],
            final=False,
            backoff=False,
            no_machine=False,
        ),
    )


def _a_published_memo() -> Ran:
    return Ran("", cast("list[Any]", [SimpleNamespace(title="memo")]), None)


def _retry_note(prompt: str) -> str:
    """What the retry adds under the contract (the contract itself says a verifier checks sources)."""
    return prompt.partition("## Your previous attempt")[2]


@pytest.fixture
def no_step_events(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """No events written; the step details a person would have read, in order."""
    details: list[str] = []

    async def step_event(*args: Any, **kwargs: Any) -> None:
        details.append(str(kwargs.get("detail", "")))

    for module in (produce, published, ladder_state):
        monkeypatch.setattr(module, "step_event", step_event)
    return details


@pytest.mark.usefixtures("no_step_events")
async def test_a_nothing_published_retry_never_mentions_a_verifier() -> None:
    """No artifact reached a reviewer, so the retry must not say one rejected it."""
    ladder = _ladder(a_spec())

    await produce.nothing_published(ladder, Ran("", [], None, undelivered="the provider failed mid-run"))

    told = _retry_note(prompt_for(a_spec(), ladder.gotchas))
    assert "the provider failed mid-run" in told
    assert "verifier" not in told and "different family" not in told and "REJECTED" not in told
    assert "reviewed draft" not in told and "from scratch" not in told, "no reviewed draft exists"


@pytest.mark.parametrize("failure", [None, FailureKind.PROVIDER_DOWN])
async def test_a_nothing_published_retry_tells_the_agent_the_fix_and_the_person_plain_words(
    no_step_events: list[str], failure: FailureKind | None
) -> None:
    """The fix is for the next attempt; the step row a person reads never says `/work` or `publish(`."""
    undelivered = (
        "the contract required a document and nothing was published - "
        "write the file to /work and call publish(type=...)"
    )
    ladder = _ladder(a_spec())

    await produce.nothing_published(ladder, Ran("", [], None, undelivered=undelivered, failure=failure))

    assert undelivered in _retry_note(prompt_for(a_spec(), ladder.gotchas))
    [said] = no_step_events
    assert said and not any(word in said for word in ("/work", "publish", "type=", "contract")), said


async def test_a_fleet_verify_retry_names_the_verifier(
    monkeypatch: pytest.MonkeyPatch, no_step_events: list[str]
) -> None:
    async def verify(session: Any, produced: Any) -> Receipts:
        return Receipts(["- `numbers:grounded` failed: 1 ungrounded"], unchecked=False)

    async def reread_epoch(session: Any, step: Any) -> None:
        return None

    monkeypatch.setattr(finish, "verify", verify)
    monkeypatch.setattr(steers, "reread_epoch", reread_epoch)
    ladder = _ladder(a_spec(), mode=TaskMode.FLEET)

    await published.deliver(ladder, _a_published_memo(), None)

    told = _retry_note(prompt_for(a_spec(), ladder.gotchas))
    assert "verifier" in told and "do not start from scratch" in told
    assert "model family" not in told, "the judge may never have spoken; only the suite is certain"
    assert "`numbers:grounded` failed" in told
    assert no_step_events[-1] == "- `numbers:grounded` failed: 1 ungrounded", (
        "the person sees findings, not the brief"
    )


async def test_a_qa_retry_names_the_editorial_review_and_no_verifier(
    monkeypatch: pytest.MonkeyPatch, no_step_events: list[str]
) -> None:
    review = SimpleNamespace(
        must_revise=True, gotcha=lambda: "- **QA (attempt 1) requires revision.** Fix exactly what is named:"
    )

    async def reviewed(*args: Any, **kwargs: Any) -> Any:
        return review

    async def steered(session: Any, task: Any, step: Any, spec: StepSpec) -> StepSpec:
        return spec

    monkeypatch.setattr(qa_ops, "review", reviewed)
    monkeypatch.setattr(steers, "steered", steered)
    ladder = _ladder(a_spec().model_copy(update={"qa": True}))

    await published.deliver(ladder, _a_published_memo(), None)

    told = _retry_note(prompt_for(a_spec(), ladder.gotchas))
    assert "editorial review" in told and "do not start from scratch" in told
    assert "requires revision" in told
    assert "verifier" not in told and "different family" not in told
    assert no_step_events[-1] == review.gotcha(), "the person sees the review, not the brief"


# What a guarded run's death owes the step's next attempt (agent loop spec 3.4): how it takes up the
# run that died on its call log (`pickup`), why it stopped, and the steers taken since - on the
# record, so every park and pace between the rungs keeps it.


def _steer(text: str) -> SteerNote:
    return SteerNote(command_id=uuid.uuid4(), epoch=1, author="Ada", text=text, by_creator=True)


def test_a_dead_run_is_owed_on_the_record_through_every_park() -> None:
    step = PlanStep(resume_state={"handbacks": ["park:g"]})
    owe_run(step, OwedRun(pickup="continue", reason="the model answered with nothing usable"))
    step.resume_state = with_record(step, {"budget_gate": True, "gotchas": ["- it died"]})  # the Bursar parks
    step.resume_state = json.loads(json.dumps(step.resume_state))  # the row's JSONB round trip
    step.resume_state = with_record(step, None)  # the answered gate clears the park's own state
    owed = owed_run(step)
    assert owed is not None and (owed.pickup, owed.reason) == (
        "continue",
        "the model answered with nothing usable",
    )
    owe_run(step, None)  # the attempt's receipt takes it
    assert owed_run(step) is None and step.resume_state == {"handbacks": ["park:g"]}


def test_steers_are_owed_once_each_until_a_run_hears_them() -> None:
    """A rung that applies a steer and then fails before its run must not lose it - the steer is
    applied (its epoch moved), so no later rung's `fresh` will name it."""
    euros, bullets = _steer("use euros"), _steer("bullets, not prose")
    owed = OwedRun(pickup="continue", reason="it died").owe([euros])
    owed = owed.owe([euros, bullets])  # the next rung: deduplicated by the steer's command id
    assert owed.steers == (euros, bullets)
    assert OwedRun.model_validate(json.loads(owed.model_dump_json())) == owed
