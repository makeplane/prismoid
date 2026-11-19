# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The two fences (ADR-064): output a steer overtook never ships.

Fence 1, at step done: a step that finished without a steer of its turn has its output superseded and
goes again in place - an infra rung, never the model's. Fence 2, at delivery: a turn with an accepted,
unapplied steer re-drafts its sinks as NEW steps and delivers after they run. Superseded output never
reaches a mount or the chat card, and a re-run that republishes the same bytes is a new artifact.
The real `execute_step` and `deliver` run against real rows, every frame a `FakeFrame`, the model run
stubbed; a steer lands "during harvest" inside the canned run after its publish (no safe point there)."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import pytest

import prismoid.ai.agents.base as agents_base
from prismoid import storage
from prismoid.conf import settings
from prismoid.db.base import utcnow
from prismoid.db.models.actor import Actor
from prismoid.db.models.artifact import Artifact
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.plan import PlanEdge, PlanEdit, PlanStep
from prismoid.db.models.project_member import ProjectMember
from prismoid.db.models.steer import Steer
from prismoid.db.models.task import Task
from prismoid.db.models.workspace import WorkspaceRole
from prismoid.db.session import row_scope, session_scope
from prismoid.jobs import effects
from prismoid.jobs import verify as verify_job
from prismoid.operations import execution, steering
from prismoid.operations import projects as project_ops
from prismoid.operations import tasks as task_ops
from prismoid.operations import verification as verification_ops
from prismoid.operations.execution import finish, mounts
from prismoid.schemas.contract import Contract
from prismoid.schemas.events import (
    ArtifactProduced,
    PlanEdited,
    SteerApplied,
    SteerRejected,
    StepStateChanged,
    TaskPlanned,
    ToolCalled,
)
from prismoid.schemas.ids import AgentName, ProjectId, StepId, TaskId
from prismoid.schemas.outputs import ArtifactType, OutputRef
from prismoid.schemas.plan import PlanEditKind, PlanView, StepKind, StepSpec, StepState, TaskStatus
from prismoid.schemas.steering import (
    REJECT_TURN_ENDED,
    SafePoint,
    SteerNote,
    SteerState,
    held_reason,
    rerun_text,
)
from tests.support.cast import member
from tests.support.runners import scripted

pytestmark = pytest.mark.integration

Run = Callable[..., Awaitable[Any]]
EUROS = "write it in euros"


def _spec(
    title: str, domains: tuple[str, ...] = (), agent: str = "maker", *, browser: bool = False
) -> dict[str, Any]:
    return StepSpec(
        kind=StepKind.STEP,
        agent=AgentName(agent),
        browser=browser,
        title=title,
        contract=Contract(
            goal=title,
            acceptance=["it exists"],
            inputs=[],
            output=OutputRef(type=ArtifactType.DOCUMENT, title=title),
            allow_domains=list(domains),
        ),
    ).model_dump(mode="json")


async def _project_task(*, mode: str = "standard") -> tuple[uuid.UUID, uuid.UUID]:
    """Ada's running Project task with Bob an editor there. Returns (task id, Bob's id)."""
    async with session_scope() as s:
        ada, workspace = await member(s, "Ada")
        bob, _ = await member(s, "Bob", role=WorkspaceRole.MEMBER, workspace=workspace)
        project = (await project_ops.create(s, ada, name=f"f-{uuid.uuid4().hex[:6]}")).project
        s.add(ProjectMember(project_id=project.id, actor_id=bob.id, role="editor", added_by=ada.id))
        await s.flush()
        task = await task_ops.create_task(s, ada, goal="a memo", project_id=project.id, clarify="never")
        task.mode = mode
        task.transition(TaskStatus.PLANNING)
        task.transition(TaskStatus.RUNNING)
        await s.commit()
        return task.id, bob.id


async def _add_step(
    task_id: uuid.UUID,
    title: str,
    state: StepState,
    *,
    ordinal: int = 0,
    allow_domains: tuple[str, ...] = (),
    agent: str = "maker",
    browser: bool = False,
) -> uuid.UUID:
    async with row_scope(Task, task_id) as s:
        task = await s.get(Task, task_id)
        assert task is not None
        step = PlanStep(
            id=uuid.uuid4(),
            task_id=task_id,
            ordinal=ordinal,
            kind=StepKind.STEP.value,
            state=state.value,
            title=title,
            spec=_spec(title, allow_domains, agent, browser=browser),
            attempt=1 if state is StepState.DONE else 0,
            epoch=task.plan_epoch,
            turn_epoch=task.turn_epoch,
        )
        s.add(step)
        await s.commit()
        return step.id


async def _published(task_id: uuid.UUID, step_id: uuid.UUID, body: bytes) -> uuid.UUID:
    """A harvested deliverable on a done step."""
    digest = storage.put(body)
    async with row_scope(Task, task_id) as s:
        row = Artifact(
            id=uuid.uuid4(),
            lineage_id=uuid.uuid4(),
            task_id=task_id,
            step_id=step_id,
            type="document",
            title=body.decode()[:20],
            sha256=digest,
            bytes=len(body),
            storage_key=storage.key_for(digest),
            provenance={"path": f"/artifacts/{digest[:8]}.md"},
        )
        s.add(row)
        await s.commit()
        return row.id


async def _steer(actor_id: uuid.UUID, task_id: uuid.UUID, text: str = EUROS) -> Steer:
    async with row_scope(Task, task_id) as s:
        actor = await s.get(Actor, actor_id)
        assert actor is not None
        return await steering.steer(s, actor, task_id, text)


async def _retract(actor_id: uuid.UUID, task_id: uuid.UUID, command_id: uuid.UUID) -> None:
    async with row_scope(Task, task_id) as s:
        actor = await s.get(Actor, actor_id)
        assert actor is not None
        await steering.retract(s, actor, task_id, command_id)


async def _run(task_id: uuid.UUID, step_id: uuid.UUID) -> str:
    async with row_scope(Task, task_id) as session:
        return await execution.execute_step(session, task_id, step_id)


async def _deliver(task_id: uuid.UUID) -> execution.Delivery:
    async with row_scope(Task, task_id) as session:
        delivery = await execution.deliver(session, task_id, "summary", turn=1)
        await session.commit()
        return delivery


async def _planned(task_id: uuid.UUID, route: str, source: str = "detector") -> None:
    """The turn's routed record (`task.planned`), as planning or an expansion writes it."""
    async with row_scope(Task, task_id) as s:
        task = await s.get(Task, task_id)
        assert task is not None
        seq = await task.next_seq(s)
        await TaskEvent.append(
            s,
            TaskPlanned(
                seq=seq,
                task_id=TaskId(task_id),
                project_id=ProjectId(task.project_id) if task.project_id else None,
                at=utcnow(),
                plan=PlanView(),
                route=route,
                route_source=source,
            ),
        )
        await s.commit()


async def _sinks(task_id: uuid.UUID) -> list[uuid.UUID]:
    async with row_scope(Task, task_id) as s:
        task = await s.get(Task, task_id)
        assert task is not None
        return [row.id for row in await steering.turn_sinks(s, task)]


async def _step(task_id: uuid.UUID, step_id: uuid.UUID) -> PlanStep:
    async with row_scope(Task, task_id) as s:
        step = await s.get(PlanStep, step_id)
        assert step is not None
        s.expunge(step)
        return step


async def _steps(task_id: uuid.UUID) -> list[PlanStep]:
    async with row_scope(Task, task_id) as s:
        rows = await PlanStep.for_task(s, task_id)
        for row in rows:
            s.expunge(row)
        return rows


async def _edges(task_id: uuid.UUID) -> set[tuple[uuid.UUID, uuid.UUID]]:
    async with row_scope(Task, task_id) as s:
        return {(e.from_step_id, e.to_step_id) for e in await PlanEdge.for_task(s, task_id)}


async def _artifacts(task_id: uuid.UUID) -> list[Artifact]:
    async with row_scope(Task, task_id) as s:
        rows = await Artifact.for_task(s, task_id)
        for row in rows:
            s.expunge(row)
        return rows


async def _task(task_id: uuid.UUID) -> Task:
    async with row_scope(Task, task_id) as s:
        task = await s.get(Task, task_id)
        assert task is not None
        s.expunge(task)
        return task


async def _steers(task_id: uuid.UUID) -> list[Steer]:
    async with row_scope(Task, task_id) as s:
        rows = await Steer.since(s, task_id, 0)
        for row in rows:
            s.expunge(row)
        return rows


async def _events[E](task_id: uuid.UUID, kind: type[E]) -> list[E]:
    async with row_scope(Task, task_id) as s:
        return [e for e in await TaskEvent.replay(s, task_id, since=0) if isinstance(e, kind)]


async def _no_previews(frame: Any, manifest: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    return {}, {}


@pytest.fixture
def stub_run(monkeypatch: pytest.MonkeyPatch) -> Callable[[Run], None]:
    """No agent at all: `build()` and the run are stubs, so no safe point ever fires."""
    monkeypatch.setattr("prismoid.operations.execution.harvest.extract_previews", _no_previews)

    def use(run: Run) -> None:
        monkeypatch.setattr(agents_base, "build", scripted(run))

    return use


async def _publish(frame: Any, body: bytes) -> str:
    await frame.write_bytes("/work/memo.md", body)
    await frame.publish("/work/memo.md", "document", "memo", ())
    return f"wrote {len(body)} bytes"


# ── fence 1: at step done ─────────────────────────────────────────────────────────────────────


async def test_fence_1_supersedes_and_reruns_in_place_without_a_rung(
    fake_frames: Any, silent_dispatcher: None, stub_run: Callable[[Run], None]
) -> None:
    task_id, bob_id = await _project_task()
    step_id = await _add_step(task_id, "memo", StepState.READY)
    prompts: list[str] = []

    async def run(agent: Any, prompt: Any, deps: Any, **kwargs: Any) -> Any:
        prompts.append(prompt)
        if len(prompts) == 1:
            said = await _publish(deps.frame, b"# memo in dollars")
            await _steer(bob_id, task_id)  # after the publish, with no safe point left
            return SimpleNamespace(output=said)
        return SimpleNamespace(output=await _publish(deps.frame, b"# memo in euros"))

    stub_run(run)
    await _run(task_id, step_id)

    step = await _step(task_id, step_id)
    assert step.step_state is StepState.DONE
    assert (step.attempt, step.infra_attempts, step.model_attempts()) == (2, 1, 1), "off the ladder"
    first, second = await _artifacts(task_id)
    assert (first.role, second.role) == ("superseded", "deliverable")
    (row,) = await _steers(task_id)
    assert (row.state, row.safe_point, row.applied_step_id) == ("applied", "rerun", step_id)
    assert step.epoch == row.epoch
    (applied,) = await _events(task_id, SteerApplied)
    assert (applied.safe_point, applied.superseded, applied.step_id) == (SafePoint.RERUN, [first.id], step_id)
    assert "**Bob steered** after your last attempt finished: write it in euros" in prompts[1]
    assert "It is at /task/inputs/memo.md (read-only): revise it in /work and publish again" in prompts[1]
    assert EUROS not in prompts[0]
    retried = [e for e in await _events(task_id, StepStateChanged) if e.state is StepState.RETRYING]
    assert [e.detail for e in retried] == ["a steer arrived as it finished"]


async def test_fence_1_on_a_fresh_frame_reads_its_superseded_output_from_inputs(
    fake_frames: Any,
    silent_dispatcher: None,
    stub_run: Callable[[Run], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With Machines off every attempt is a fresh frame: `/work` starts empty, so the re-run finds
    the output it revises where a follow-up finds its prior turn - read-only under
    `/task/inputs/` - and its prompt says so (told to revise it in /work,
    the re-run found nothing there and burned an attempt)."""
    monkeypatch.setattr(settings.machines, "enabled", False)
    task_id, bob_id = await _project_task()
    step_id = await _add_step(task_id, "memo", StepState.READY)
    prompts: list[str] = []
    mounted: list[dict[str, bytes]] = []

    async def run(agent: Any, prompt: Any, deps: Any, **kwargs: Any) -> Any:
        prompts.append(prompt)
        mounted.append({path: data for path, (data, _) in deps.frame.files.items()})
        if len(prompts) == 1:
            said = await _publish(deps.frame, b"# memo in dollars")
            await _steer(bob_id, task_id)
            return SimpleNamespace(output=said)
        return SimpleNamespace(output=await _publish(deps.frame, b"# memo in euros"))

    stub_run(run)
    await _run(task_id, step_id)

    assert len(fake_frames.created) == 2, "a fresh frame for the re-run"
    assert "/work/memo.md" not in mounted[1], "its /work starts empty"
    assert mounted[1]["/task/inputs/memo.md"] == b"# memo in dollars", "the superseded output is mounted"
    assert "It is at /task/inputs/memo.md (read-only)" in prompts[1]
    assert "/task/inputs/memo.md" not in mounted[0]
    first, second = await _artifacts(task_id)
    assert (first.role, second.role) == ("superseded", "deliverable")


async def test_fence_1_counts_a_sibling_apply_as_unseen(
    fake_frames: Any, silent_dispatcher: None, stub_run: Callable[[Run], None]
) -> None:
    """A steer a sibling already took is still one THIS step never saw."""
    task_id, bob_id = await _project_task()
    step_id = await _add_step(task_id, "memo", StepState.READY)
    sibling_id = await _add_step(task_id, "chart", StepState.RUNNING, ordinal=1)
    calls: list[int] = []

    async def run(agent: Any, prompt: Any, deps: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            said = await _publish(deps.frame, b"# memo in dollars")
            row = await _steer(bob_id, task_id)
            async with row_scope(Task, task_id) as s:  # the sibling's safe point took it
                await Steer.settle(s, task_id, [row.command_id], to=SteerState.APPLIED, step_id=sibling_id)
                await s.commit()
            return SimpleNamespace(output=said)
        return SimpleNamespace(output=await _publish(deps.frame, b"# memo in euros"))

    stub_run(run)
    await _run(task_id, step_id)

    step = await _step(task_id, step_id)
    assert step.step_state is StepState.DONE and step.attempt == 2 and step.model_attempts() == 1
    (row,) = await _steers(task_id)
    assert row.applied_step_id == sibling_id, "the first application stands on the row"
    (rerun,) = await _events(task_id, SteerApplied)
    assert rerun.safe_point is SafePoint.RERUN and rerun.step_id == step_id and len(rerun.superseded) == 1


async def test_a_rerun_that_republishes_the_same_bytes_is_a_new_artifact(
    fake_frames: Any, silent_dispatcher: None, stub_run: Callable[[Run], None]
) -> None:
    """Superseded output is never taken back: a byte-identical re-publish is a NEW artifact (a fresh row
    on the same blob, one `artifact.produced`); the old row stays superseded and nothing is re-sent for
    it, since a re-announced id would fire a produced-triggered Routine twice."""
    task_id, bob_id = await _project_task()
    step_id = await _add_step(task_id, "memo", StepState.READY)
    calls: list[int] = []

    async def run(agent: Any, prompt: Any, deps: Any, **kwargs: Any) -> Any:
        calls.append(1)
        said = await _publish(deps.frame, b"# memo, already in euros")
        if len(calls) == 1:
            await _steer(bob_id, task_id)
        return SimpleNamespace(output=said)

    stub_run(run)
    await _run(task_id, step_id)

    assert len(calls) == 2
    rows = await _artifacts(task_id)
    old = next(a for a in rows if a.role == "superseded")
    new = next(a for a in rows if a.role == "deliverable")
    assert len(rows) == 2 and old.id != new.id and old.sha256 == new.sha256
    assert old.storage_key == new.storage_key, "the blob is content-addressed: one blob, two rows"
    (rerun,) = await _events(task_id, SteerApplied)
    assert rerun.superseded == [old.id]
    produced = await _events(task_id, ArtifactProduced)
    assert [e.artifact_id for e in produced] == [old.id, new.id], "one frame each, none re-sent"
    assert produced[1].role == "deliverable" and produced[1].seq > rerun.seq
    assert (await _step(task_id, step_id)).step_state is StepState.DONE


async def test_fence_1_runs_again_before_the_fleet_lane_ships(
    fake_frames: Any,
    silent_dispatcher: None,
    stub_run: Callable[[Run], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The in-line review sits between harvest and done: a steer during it is caught right before done."""
    task_id, bob_id = await _project_task(mode="fleet")
    step_id = await _add_step(task_id, "memo", StepState.READY)
    reviews: list[int] = []

    async def verify(session: Any, produced: Any) -> finish.Receipts:
        reviews.append(1)
        await session.commit()
        if len(reviews) == 1:
            await _steer(bob_id, task_id)
        return finish.Receipts([], unchecked=False)

    async def run(agent: Any, prompt: Any, deps: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(output=await _publish(deps.frame, f"# memo {len(reviews)}".encode()))

    monkeypatch.setattr(finish, "verify", verify)
    stub_run(run)
    await _run(task_id, step_id)

    step = await _step(task_id, step_id)
    assert step.step_state is StepState.DONE and (step.attempt, step.model_attempts()) == (2, 1)
    assert len(reviews) == 2
    first, second = await _artifacts(task_id)
    assert (first.role, second.role) == ("superseded", "deliverable")
    (applied,) = await _events(task_id, SteerApplied)
    assert applied.safe_point is SafePoint.RERUN and applied.superseded == [first.id]


# ── fence 2: at delivery ──────────────────────────────────────────────────────────────────────


async def test_fence_2_reopens_the_sinks_and_delivers_after(
    fake_frames: Any, silent_dispatcher: None, stub_run: Callable[[Run], None]
) -> None:
    task_id, bob_id = await _project_task()
    research = await _add_step(task_id, "research", StepState.DONE)
    memo = await _add_step(task_id, "memo", StepState.DONE, ordinal=1)
    async with row_scope(Task, task_id) as s:
        s.add(PlanEdge(task_id=task_id, from_step_id=research, to_step_id=memo, kind="depends_on"))
        await s.commit()
    notes_id = await _published(task_id, research, b"# research notes")
    dollars_id = await _published(task_id, memo, b"# memo in dollars")
    row = await _steer(bob_id, task_id)

    delivery = await _deliver(task_id)
    assert (delivery.verdict, delivery.replaced) == ("reopened", (memo,)), "it names the sink it replaced"

    task = await _task(task_id)
    assert task.state is TaskStatus.RUNNING, "not delivered"
    old, kept_memo, redraft = await _steps(task_id)
    assert (old.id, kept_memo.id) == (research, memo)
    assert kept_memo.step_state is StepState.DONE, "the old sink stays done"
    assert redraft.step_state is StepState.READY and redraft.title == "memo"
    assert (redraft.epoch, redraft.turn_epoch, redraft.ordinal) == (task.plan_epoch, task.turn_epoch, 2)
    note = SteerNote(command_id=row.command_id, epoch=row.epoch, author="Bob", text=EUROS, by_creator=False)
    respec = StepSpec.model_validate(redraft.spec)
    (dollars,) = [a for a in await _artifacts(task_id) if a.id == dollars_id]
    dollars_at = f"/task/inputs/{mounts.upstream_filename(dollars)}"
    assert respec.redrafts == memo, "the re-draft names the sink it redoes"
    assert respec.contract is not None and respec.contract.goal == "memo\n\n" + rerun_text(
        [note], [dollars_at]
    )
    edges = await _edges(task_id)
    assert (research, redraft.id) in edges, "the sink's incoming edges are copied"
    assert (memo, redraft.id) in edges, "the superseded sink is upstream of its re-draft, never a sink again"
    roles = {a.id: a.role for a in await _artifacts(task_id)}
    assert roles == {notes_id: "deliverable", dollars_id: "superseded"}
    (settled,) = await _steers(task_id)
    assert (settled.state, settled.safe_point, settled.applied_step_id) == ("applied", "rerun", memo)
    (applied,) = await _events(task_id, SteerApplied)
    assert (applied.safe_point, applied.superseded, applied.step_id) == (SafePoint.RERUN, [dollars_id], memo)
    reason = "Re-planned for Bob's steer: 'write it in euros'"
    (edited,) = [e for e in await _events(task_id, PlanEdited) if e.edit.kind is PlanEditKind.REPLAN]
    assert (edited.edit.kind, edited.edit.reason, edited.edit.added) == (
        PlanEditKind.REPLAN,
        reason,
        [redraft.id],
    )
    async with row_scope(Task, task_id) as s:
        live_task = await s.get(Task, task_id)
        new_row = await s.get(PlanStep, redraft.id)
        assert live_task is not None and new_row is not None
        (edit,) = await PlanEdit.of_kind(s, task_id, PlanEditKind.REPLAN.value)
        assert (edit.reason, edit.actor_id, edit.payload["redrafted"]) == (
            reason,
            None,
            {str(memo): str(redraft.id)},
        )
        assert [a.id for a in await mounts.upstream_artifacts(s, live_task, new_row)] == [notes_id]
        assert await steering.turn_sinks(s, live_task) == [], "the replaced sink is not one any more"
        shelf = await Artifact.by_agent(s, "maker", creator_actor_id=live_task.creator_actor_id)
        assert [a.id for a, _ in shelf] == [notes_id], "the bot page's shelf drops superseded output"

    mounted: dict[str, bytes] = {}

    async def run(agent: Any, prompt: Any, deps: Any, **kwargs: Any) -> Any:
        mounted.update({path: data for path, (data, _) in deps.frame.files.items()})
        return SimpleNamespace(output=await _publish(deps.frame, b"# memo in euros"))

    stub_run(run)
    await _run(task_id, redraft.id)
    assert (await _step(task_id, redraft.id)).step_state is StepState.DONE
    assert mounted[dollars_at] == b"# memo in dollars", "the re-draft reads the output it redoes"
    async with row_scope(Task, task_id) as s:
        live_task = await s.get(Task, task_id)
        assert live_task is not None
        assert [s_.id for s_ in await steering.turn_sinks(s, live_task)] == [redraft.id]

    assert (await _deliver(task_id)).verdict == "delivered"
    assert (await _task(task_id)).state is TaskStatus.DELIVERED


async def test_a_steer_with_no_sink_to_take_it_is_rejected_and_the_task_delivers(
    silent_dispatcher: None,
) -> None:
    task_id, bob_id = await _project_task()
    await _add_step(task_id, "memo", StepState.SKIPPED)
    row = await _steer(bob_id, task_id)

    assert (await _deliver(task_id)).verdict == "delivered"

    (settled,) = await _steers(task_id)
    assert (settled.state, settled.reason) == ("rejected", REJECT_TURN_ENDED)
    (rejected,) = await _events(task_id, SteerRejected)
    assert (rejected.command_id, rejected.reason) == (row.command_id, REJECT_TURN_ENDED)


async def test_a_task_from_before_steering_rejects_instead_of_reopening(silent_dispatcher: None) -> None:
    task_id, bob_id = await _project_task()
    memo = await _add_step(task_id, "memo", StepState.DONE)
    await _published(task_id, memo, b"# memo in dollars")
    await _steer(bob_id, task_id)
    async with row_scope(Task, task_id) as s:  # a turn opened before 1c carries no epoch
        task = await s.get(Task, task_id)
        step = await s.get(PlanStep, memo)
        assert task is not None and step is not None
        task.turn_epoch, step.turn_epoch = 0, 0
        await s.commit()

    assert (await _deliver(task_id)).verdict == "delivered"
    (settled,) = await _steers(task_id)
    assert (settled.state, settled.reason) == ("rejected", REJECT_TURN_ENDED)
    assert len(await _steps(task_id)) == 1


async def test_a_stale_or_settled_delivery_is_skipped(silent_dispatcher: None) -> None:
    task_id, _ = await _project_task()
    await _add_step(task_id, "memo", StepState.DONE)
    async with row_scope(Task, task_id) as s:
        assert (await execution.deliver(s, task_id, "summary", turn=2)).verdict == "skipped", "a stale turn"
    assert await effects.deliver_task(str(task_id), "summary", 1) == {"verdict": "delivered", "replaced": []}
    assert (await effects.deliver_task(str(task_id), "summary", 1))["verdict"] == "skipped", "a replay"


async def test_an_escalated_turns_answerer_is_never_redrafted(silent_dispatcher: None) -> None:
    """A turn that escalated holds the answerer's express step beside the plan it drafted:
    the plan is what the turn delivers - re-running the answerer would plan the turn twice. Keyed on
    the hatch's own record (`plan_edits{hatch}` names the step), so a later `task.planned` - the
    long-form or scatter expansion's - does not bring the answerer back."""
    task_id, bob_id = await _project_task()
    answerer = await _add_step(task_id, "answer", StepState.DONE)
    async with row_scope(Task, task_id) as s:
        step = await s.get(PlanStep, answerer)
        assert step is not None
        step.spec = {**step.spec, "lane_role": "express"}
        await s.commit()
    await _planned(task_id, "express")
    assert await _sinks(task_id) == [answerer], "an express turn delivers its answer"

    async with row_scope(Task, task_id) as s:  # the answerer escalates: the hatch's record
        s.add(
            PlanEdit(
                task_id=task_id,
                kind=PlanEditKind.HATCH.value,
                reason="a document",
                payload={"step_id": str(answerer)},
            )
        )
        await s.commit()
    await _planned(task_id, "standard", source="hatch")
    outline = await _add_step(task_id, "outline", StepState.DONE, ordinal=1)
    book = await _add_step(task_id, "book", StepState.DONE, ordinal=2)
    async with row_scope(Task, task_id) as s:
        s.add(PlanEdge(task_id=task_id, from_step_id=outline, to_step_id=book, kind="depends_on"))
        await s.commit()
    await _planned(task_id, "standard", source="planner")  # the long-form expansion's record
    await _steer(bob_id, task_id)

    delivery = await _deliver(task_id)

    assert (delivery.verdict, delivery.replaced) == ("reopened", (book,))
    assert [s.title for s in await _steps(task_id)] == ["answer", "outline", "book", "book"]


# ── a withdrawn steer ─────────────────────────────────────────────────────────────────────────


async def test_a_retracted_steer_trips_no_fence(
    fake_frames: Any, silent_dispatcher: None, stub_run: Callable[[Run], None]
) -> None:
    task_id, bob_id = await _project_task()
    step_id = await _add_step(task_id, "memo", StepState.READY)

    async def run(agent: Any, prompt: Any, deps: Any, **kwargs: Any) -> Any:
        said = await _publish(deps.frame, b"# memo in dollars")
        row = await _steer(bob_id, task_id)
        await _retract(bob_id, task_id, row.command_id)
        return SimpleNamespace(output=said)

    stub_run(run)
    await _run(task_id, step_id)

    step = await _step(task_id, step_id)
    assert step.step_state is StepState.DONE and step.attempt == 1
    (only,) = await _artifacts(task_id)
    assert only.role == "deliverable"
    assert (await _deliver(task_id)).verdict == "delivered"
    assert await _events(task_id, SteerApplied) == []


# ── never twice in the world ─────────────────────────────────────────────────────


@pytest.mark.parametrize("route", ["action", "navigate"])
async def test_an_action_turns_step_finishes_past_a_steer(
    route: str, fake_frames: Any, silent_dispatcher: None, stub_run: Callable[[Run], None]
) -> None:
    """Fence 1 never re-runs a step of an action or navigate turn: running it again repeats what it
    did in the world. The step finishes; delivery says the steer came too late."""
    task_id, bob_id = await _project_task()
    await _planned(task_id, route)
    step_id = await _add_step(task_id, "send it", StepState.READY)
    calls: list[int] = []

    async def run(agent: Any, prompt: Any, deps: Any, **kwargs: Any) -> Any:
        calls.append(1)
        said = await _publish(deps.frame, b"# sent")
        if len(calls) == 1:
            await _steer(bob_id, task_id)
        return SimpleNamespace(output=said)

    stub_run(run)
    await _run(task_id, step_id)

    step = await _step(task_id, step_id)
    assert (len(calls), step.step_state, step.attempt) == (1, StepState.DONE, 1)
    (only,) = await _artifacts(task_id)
    assert only.role == "deliverable"
    (row,) = await _steers(task_id)
    assert row.state == "accepted"


@pytest.mark.parametrize("route", ["action", "navigate"])
async def test_an_action_turn_rejects_at_delivery_instead_of_redrafting(
    route: str, silent_dispatcher: None
) -> None:
    task_id, bob_id = await _project_task()
    await _planned(task_id, route)
    sent = await _add_step(task_id, "send it", StepState.DONE)
    await _published(task_id, sent, b"# sent")
    await _steer(bob_id, task_id)

    assert (await _deliver(task_id)).verdict == "delivered"

    (settled,) = await _steers(task_id)
    assert (settled.state, settled.reason) == ("rejected", REJECT_TURN_ENDED)
    assert [s.id for s in await _steps(task_id)] == [sent]
    (only,) = await _artifacts(task_id)
    assert only.role == "deliverable"


# ── never twice outside the task ─────────────────────────────────────────────────


async def _journaled(
    task_id: uuid.UUID,
    step_id: uuid.UUID,
    tool: str,
    *,
    attempt: int,
    ok: bool = True,
    reason: str = "ReadTimeout: the server took too long",
) -> None:
    """A call this step attempt journaled, as the record writes it (a failed one with its error)."""
    async with row_scope(Task, task_id) as s:
        task = await s.get(Task, task_id)
        assert task is not None
        seq = await task.next_seq(s)
        await TaskEvent.append(
            s,
            ToolCalled(
                seq=seq,
                task_id=TaskId(task_id),
                step_id=StepId(step_id),
                project_id=ProjectId(task.project_id) if task.project_id else None,
                at=utcnow(),
                tool=tool,
                ok=ok,
                summary="Linear · args: title",
                denied_reason="" if ok else reason,
                attempt=attempt,
            ),
        )
        await s.commit()


@pytest.mark.parametrize(
    ("tool", "ok", "allow_domains", "again"),
    [
        ("linear_create_issue", True, (), False),  # a connector's tool: it acted outside
        ("linear_list_issues", True, (), False),  # a connector read counts too (conservative)
        ("linear_create_issue", False, (), False),  # a call that failed may have acted first
        ("browser.snapshot", True, (), False),  # a browser action, a snapshot too
        ("routine.create", True, (), False),  # a routine outlives the task: twice is two routines
        ("web.fetch", True, (), True),  # the web door's read stays inside: it may run again
        ("web.fetch", False, (), True),
        ("connector.linear", False, (), True),  # a connector the mount dropped: nothing was called
        ("frame.shell", True, (), True),  # a frame with no route out
        ("frame.shell", True, ("hooks.example.com",), False),  # a shell that could post
    ],
)
async def test_fence_1_never_reruns_a_step_that_acted_outside_the_task(
    tool: str,
    ok: bool,
    allow_domains: tuple[str, ...],
    again: bool,
    fake_frames: Any,
    silent_dispatcher: None,
    stub_run: Callable[[Run], None],
) -> None:
    """A step that acted or may have acted outside (a connector, the browser, a routine, a shell with a route
    out) finishes past a steer and delivery says it came too late; one that stayed inside goes round again."""
    task_id, bob_id = await _project_task()
    step_id = await _add_step(task_id, "file it", StepState.READY, allow_domains=allow_domains)
    calls: list[int] = []

    async def run(agent: Any, prompt: Any, deps: Any, **kwargs: Any) -> Any:
        calls.append(1)
        failed = "" if ok else "ReadTimeout: the server took too long"
        await deps.journal_tool(tool, ok, "args: title", 12, failed, f"c{len(calls)}")
        said = await _publish(deps.frame, b"# filed")
        if len(calls) == 1:
            await _steer(bob_id, task_id)
        return SimpleNamespace(output=said)

    stub_run(run)
    await _run(task_id, step_id)

    step = await _step(task_id, step_id)
    (row,) = await _steers(task_id)
    if again:
        assert (len(calls), step.attempt, row.state) == (2, 2, "applied")
        return
    assert (len(calls), step.step_state, step.attempt) == (1, StepState.DONE, 1)
    (only,) = await _artifacts(task_id)
    assert only.role == "deliverable"
    assert row.state == "accepted"
    assert (await _deliver(task_id)).verdict == "delivered"
    (settled,) = await _steers(task_id)
    assert (settled.state, settled.reason) == ("rejected", REJECT_TURN_ENDED)
    assert [s.id for s in await _steps(task_id)] == [step_id], "nothing re-drafted"


@pytest.mark.parametrize("ok", [True, False])
async def test_fence_2_rejects_when_any_sink_that_missed_the_steer_acted_outside(
    ok: bool, silent_dispatcher: None
) -> None:
    """One sink called a connector (even a failed call may have acted), so no sink is re-drafted: the
    steer is rejected for the whole turn, never applied to half of it, and the turn delivers."""
    task_id, bob_id = await _project_task()
    await _planned(task_id, "standard", source="planner")
    memo = await _add_step(task_id, "memo", StepState.DONE)
    filed = await _add_step(task_id, "file it", StepState.DONE, ordinal=1)
    await _published(task_id, memo, b"# memo in dollars")
    await _published(task_id, filed, b"# filed")
    await _journaled(task_id, filed, "linear_create_issue", attempt=1, ok=ok)
    await _steer(bob_id, task_id)

    assert (await _deliver(task_id)).verdict == "delivered"

    (settled,) = await _steers(task_id)
    assert (settled.state, settled.reason) == ("rejected", REJECT_TURN_ENDED)
    assert [s.id for s in await _steps(task_id)] == [memo, filed]
    assert {a.role for a in await _artifacts(task_id)} == {"deliverable"}, "nothing superseded"


@pytest.mark.parametrize(
    ("agent", "browser", "allow_domains", "verdict"),
    [
        ("maker", False, (), "reopened"),  # a shell in a frame with no route out stayed inside
        ("maker", False, ("hooks.example.com",), "delivered"),  # its contract gave the frame a route out
        ("operator", True, (), "delivered"),  # a browser frame reaches the owner's allowed sites
    ],
)
async def test_fence_2_reads_every_host_the_sinks_frame_could_reach(
    agent: str, browser: bool, allow_domains: tuple[str, ...], verdict: str, silent_dispatcher: None
) -> None:
    """The step's effective egress counts, not only its contract's domains: a browser frame always allows
    the owner's sites and the identity providers, so its shell could have reached the world and the sink
    is never re-drafted. A shell in a frame with no route out stayed inside."""
    task_id, bob_id = await _project_task()
    sink = await _add_step(
        task_id, "build it", StepState.DONE, allow_domains=allow_domains, agent=agent, browser=browser
    )
    await _published(task_id, sink, b"# built")
    await _journaled(task_id, sink, "frame.shell", attempt=1)
    await _steer(bob_id, task_id)

    assert (await _deliver(task_id)).verdict == verdict

    (row,) = await _steers(task_id)
    if verdict == "reopened":
        assert row.state == "applied"
        return
    assert (row.state, row.reason) == ("rejected", REJECT_TURN_ENDED)
    assert [s.id for s in await _steps(task_id)] == [sink], "nothing re-drafted"


async def test_a_call_a_steer_held_never_counts_as_acting_outside(silent_dispatcher: None) -> None:
    """A call chosen before a steer is held, never run: its journal row (the model's own
    tool name, "held: Bob steered") is no call at all for the outside-the-task check. A builder whose first
    call Bob's steer held read as having acted outside - the raw name is no known tool - so a later
    missed steer was rejected instead of re-drafting the sink."""
    task_id, bob_id = await _project_task()
    memo = await _add_step(task_id, "memo", StepState.DONE)
    await _published(task_id, memo, b"# memo in dollars")
    note = SteerNote(command_id=uuid.uuid4(), author="Bob", text=EUROS, epoch=2, by_creator=False)
    for tool in ("read_file", "linear_create_issue"):  # a frame tool's raw name, a connector's
        await _journaled(task_id, memo, tool, attempt=1, ok=False, reason=held_reason([note]))
    await _steer(bob_id, task_id)

    assert (await _deliver(task_id)).verdict == "reopened"


async def test_fence_2_reads_only_the_superseded_attempts_calls(silent_dispatcher: None) -> None:
    """The connector call was an earlier attempt's: the attempt the steer overtook made none, so
    the sink is re-drafted as before."""
    task_id, bob_id = await _project_task()
    memo = await _add_step(task_id, "memo", StepState.DONE)
    async with row_scope(Task, task_id) as s:
        step = await s.get(PlanStep, memo)
        assert step is not None
        step.attempt = 2
        await s.commit()
    await _published(task_id, memo, b"# memo in dollars")
    await _journaled(task_id, memo, "linear_create_issue", attempt=1)
    await _steer(bob_id, task_id)

    assert (await _deliver(task_id)).verdict == "reopened"


# ── a superseded artifact's receipt ───────────────────────────────────────────────────────────


async def test_a_superseded_artifacts_receipt_is_never_signed(
    silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `verify_artifact` job runs after the step's commit, and a steer may have superseded the
    artifact by then: the receipt returns at once, judging nothing and spending nothing."""
    task_id, _ = await _project_task()
    memo = await _add_step(task_id, "memo", StepState.DONE)
    artifact_id = await _published(task_id, memo, b"# memo in dollars")
    async with row_scope(Task, task_id) as s:
        await Artifact.supersede(s, [artifact_id])
        await s.commit()

    async def never(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a superseded artifact is never judged")

    monkeypatch.setattr(verification_ops, "verify_artifact", never)
    assert await verify_job._sign_receipt(str(artifact_id)) == "superseded"  # pyright: ignore[reportPrivateUsage]


# ── delivery reads the epoch under its lock; a re-run of it answers as it did ───────────────


async def test_a_steer_landing_before_the_lock_is_the_redrafts_epoch(
    silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A steer that commits just before delivery takes the task's row lock must not leave the
    re-draft a stale epoch, or the next boundary would re-plan the step it just drafted: delivery
    reads the task only under its lock."""
    task_id, bob_id = await _project_task()
    await _planned(task_id, "standard", source="lead")
    await _add_step(task_id, "memo", StepState.DONE)
    landed: list[Steer] = []
    armed = [True]
    real_held = Task.held.__func__

    async def held(cls: type[Task], session: Any, tid: uuid.UUID) -> Task:
        if armed:  # once: as delivery reaches for the lock
            armed.clear()
            landed.append(await _steer(bob_id, task_id))
        return await real_held(cls, session, tid)

    async def no_lead(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the boundary re-planned a step drafted under the newest steer")

    monkeypatch.setattr(Task, "held", classmethod(held))
    monkeypatch.setattr("prismoid.operations.turn.replan._run", no_lead)

    assert (await _deliver(task_id)).verdict == "reopened"

    (row,) = landed
    redraft = (await _steps(task_id))[-1]
    assert redraft.step_state is StepState.READY and redraft.epoch == row.epoch
    assert await effects.steer_boundary(str(task_id)) is False


async def test_a_delivery_run_again_after_its_reopen_answers_reopened(silent_dispatcher: None) -> None:
    """An effect is at-least-once: a re-run after the reopen committed finds the never-run re-draft
    and answers as the first run did, instead of refusing to deliver an unfinished step."""
    task_id, bob_id = await _project_task()
    memo = await _add_step(task_id, "memo", StepState.DONE)
    await _published(task_id, memo, b"# memo in dollars")
    await _steer(bob_id, task_id)

    first = await _deliver(task_id)
    again = await _deliver(task_id)

    assert first == again == execution.Delivery("reopened", (memo,))
    assert len(await _steps(task_id)) == 2, "no second re-draft"


# ── a review re-entry reads only live output ──────────────────────────────────────────────────


class _WorkerDied(Exception):
    pass


async def test_a_review_reentry_verifies_only_the_live_attempts_output(
    fake_frames: Any,
    silent_dispatcher: None,
    stub_run: Callable[[Run], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id, bob_id = await _project_task(mode="fleet")
    step_id = await _add_step(task_id, "memo", StepState.READY)
    calls: list[int] = []
    reviewed: list[list[uuid.UUID]] = []

    async def run(agent: Any, prompt: Any, deps: Any, **kwargs: Any) -> Any:
        calls.append(1)
        said = await _publish(deps.frame, f"# memo {len(calls)}".encode())
        if len(calls) == 1:
            await _steer(bob_id, task_id)  # fence 1 supersedes attempt 1
        return SimpleNamespace(output=said)

    async def verify(session: Any, produced: list[Artifact]) -> finish.Receipts:
        reviewed.append([a.id for a in produced])
        if len(reviewed) == 1:
            raise _WorkerDied  # attempt 2 is VERIFYING when the worker dies
        await session.commit()
        return finish.Receipts([], unchecked=False)

    monkeypatch.setattr(finish, "verify", verify)
    stub_run(run)
    with pytest.raises(_WorkerDied):
        await _run(task_id, step_id)
    assert (await _step(task_id, step_id)).step_state is StepState.VERIFYING

    await _run(task_id, step_id)  # the re-entry: the review only

    first, second = await _artifacts(task_id)
    assert (first.role, second.role) == ("superseded", "deliverable")
    assert reviewed == [[second.id], [second.id]]
    assert (await _step(task_id, step_id)).step_state is StepState.DONE
