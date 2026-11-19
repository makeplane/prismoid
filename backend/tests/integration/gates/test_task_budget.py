# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""B3: raising a task's budget - who may, what the record says, and what the gate learns."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update

from prismoid.ai.rules.budget import BudgetVerdict, over
from prismoid.api.app import create_app
from prismoid.conf import settings
from prismoid.db.models.actor import Actor
from prismoid.db.models.api_key import ApiKey
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.gate import Gate
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.project import Project
from prismoid.db.models.project_member import ProjectMember
from prismoid.db.models.task import Task
from prismoid.db.models.usage import ProjectUsageDaily
from prismoid.db.models.workspace import WorkspaceRole
from prismoid.db.session import row_scope, session_scope
from prismoid.jobs import effects
from prismoid.operations import auth as auth_ops
from prismoid.operations import gates as gate_ops
from prismoid.operations import projects as project_ops
from prismoid.operations import steering
from prismoid.operations import tasks as task_ops
from prismoid.schemas.contract import Contract
from prismoid.schemas.events import TaskBudgetSet
from prismoid.schemas.ids import AgentName
from prismoid.schemas.outputs import ArtifactType
from prismoid.schemas.plan import (
    BudgetFigures,
    GateAction,
    GateSpec,
    StepKind,
    StepSpec,
    StepState,
    TaskStatus,
)
from prismoid.schemas.sizing import SizeBucket
from tests.support.cast import member

pytestmark = pytest.mark.integration


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def client(recording_dispatcher: Any) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as c:
        yield c


async def _shared(
    scope: str = "task", *, budget: str = "0.01", verdict: BudgetVerdict | None = None
) -> tuple[uuid.UUID, str, str, str, str]:
    """A Project task with a $0.01 budget parked on "Budget reached" (the given ceiling; or the given
    budget and the verdict that parked it): the owner's, an editor's, a viewer's and an outsider's
    keys."""
    async with session_scope() as s:
        ada, workspace = await member(s, "Ada")
        bob, _ = await member(s, "Bob", role=WorkspaceRole.MEMBER, workspace=workspace)
        cy, _ = await member(s, "Cy", role=WorkspaceRole.MEMBER, workspace=workspace)
        dee, _ = await member(
            s, "Dee", role=WorkspaceRole.MEMBER, workspace=workspace
        )  # in the workspace, not the Project
        project = (await project_ops.create(s, ada, name=f"b-{uuid.uuid4().hex[:6]}")).project
        s.add(ProjectMember(project_id=project.id, actor_id=bob.id, role="editor", added_by=ada.id))
        s.add(ProjectMember(project_id=project.id, actor_id=cy.id, role="viewer", added_by=ada.id))
        task = await task_ops.create_task(s, ada, goal="capped", project_id=project.id)
        task.budget = {"usd": budget}
        task.status = TaskStatus.RUNNING.value  # the Bursar parks a RUNNING task (running -> waiting)
        await gate_ops._open_budget(  # pyright: ignore[reportPrivateUsage]
            s,
            task,
            verdict or BudgetVerdict(spent=Decimal("0.02"), limit=Decimal("0.01"), scope=scope),
            step_id=None,  # the frontier's park, not a step ladder's
        )
        keys = []
        for who in (ada, bob, cy, dee):
            key, raw = ApiKey.issue(who.id, "t", workspace_id=workspace.id)
            s.add(key)
            keys.append(raw)
        await s.commit()
    return task.id, *keys


async def test_an_editor_raises_the_budget_and_learns_the_gate_to_resume(client: AsyncClient) -> None:
    task_id, _, editor, _, _ = await _shared()
    r = await client.put(f"/v1/tasks/{task_id}/budget", json={"usd": "2"}, headers=_h(editor))
    assert r.status_code == 200, r.text
    body = r.json()
    assert Decimal(body["usd"]) == Decimal("2") and Decimal(body["previous_usd"]) == Decimal("0.01")
    async with row_scope(Task, task_id) as s:
        task = await s.get(Task, task_id)
        assert task is not None and task.budget is not None
        assert Decimal(str(task.budget["usd"])) == Decimal("2")
        gate = await Gate.open_budget_for(s, task_id)
        assert gate is not None and body["gate_id"] == str(gate.id)
        events = await TaskEvent.replay(s, task_id)
    budget_set = [e for e in events if e.type == "task.budget_set"]
    assert len(budget_set) == 1 and budget_set[0].actor is not None and budget_set[0].actor.name == "Bob"
    # Fix wave B6: what the record says the raise was - the timeline's "Bob raised the budget to
    # $2.00" reads these, so a wrong previous_usd would be a wrong sentence.
    assert budget_set[0].usd == Decimal("2.00") and budget_set[0].previous_usd == Decimal("0.01")


async def test_a_viewer_gets_403_and_an_outsider_404(client: AsyncClient) -> None:
    task_id, _, _, viewer, outsider = await _shared()
    r = await client.put(f"/v1/tasks/{task_id}/budget", json={"usd": "2"}, headers=_h(viewer))
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
    r = await client.put(f"/v1/tasks/{task_id}/budget", json={"usd": "2"}, headers=_h(outsider))
    assert r.status_code == 404


@pytest.mark.parametrize("usd", ["0", "-1", "abc", "2.345", "", None])
async def test_a_budget_that_is_not_dollars_and_cents_above_zero_is_422(
    client: AsyncClient, usd: Any
) -> None:
    """The API refuses what the dock would never send."""
    task_id, owner, _, _, _ = await _shared()
    r = await client.put(f"/v1/tasks/{task_id}/budget", json={"usd": usd}, headers=_h(owner))
    assert r.status_code == 422


async def test_a_spaceless_task_is_its_creators_alone(client: AsyncClient) -> None:
    async with session_scope() as s:
        result = await auth_ops.signup(
            s, email=f"b-{uuid.uuid4().hex[:8]}@example.com", name="Ada", password="a-long-enough-password"
        )
        actor = await s.get(Actor, result.user.actor_id)
        assert actor is not None
        task = await task_ops.create_task(s, actor, goal="mine")
        await s.commit()
    r = await client.put(f"/v1/tasks/{task.id}/budget", json={"usd": "5"}, headers=_h(result.token))
    assert r.status_code == 200 and r.json()["previous_usd"] is None and r.json()["gate_id"] is None


@pytest.mark.parametrize("scope", ["task", "project"])
async def test_the_gate_wears_the_scope_of_the_ceiling_it_hit(client: AsyncClient, scope: str) -> None:
    """A Project ceiling is the owner's to raise - the gate says which it is, in its
    spec and on its event (which now carries the reason too), so the dock picks the right face."""
    task_id, _, _, _, _ = await _shared(scope)
    async with row_scope(Task, task_id) as s:
        gate = await Gate.open_budget_for(s, task_id)
        assert gate is not None and GateSpec.model_validate(gate.spec).budget_scope == scope
        opened = [e for e in await TaskEvent.replay(s, task_id) if e.type == "gate.opened"]
        assert opened[-1].budget_scope == scope
        assert f"the {scope} budget of $0.01 is spent" in opened[-1].reason


async def test_the_cost_confirmation_wears_no_scope() -> None:
    async with session_scope() as s:
        ada, _ = await member(s, "Ada")
        task = await task_ops.create_task(s, ada, goal="a long document")
        task.status = TaskStatus.RUNNING.value
        confirm = await gate_ops._open_budget_gate(  # pyright: ignore[reportPrivateUsage]
            s, task, title=gate_ops.CONFIRM_TITLE, reason="consent"
        )
        assert GateSpec.model_validate(confirm.spec).budget_scope == ""
        await s.commit()


async def _project_spent(task_id: uuid.UUID, budget: str = "5", spent: str = "9") -> uuid.UUID:
    """The task's Project with a monthly ceiling and this month's spend: by default really over it,
    a $5 budget and $9 spent."""
    async with row_scope(Task, task_id) as s:
        task = await s.get(Task, task_id)
        assert task is not None and task.project_id is not None
        project = await s.get(Project, task.project_id)
        assert project is not None
        project.budget_usd = Decimal(budget)
        await ProjectUsageDaily.bump(
            s, project_id=project.id, day=date.today(), kind="total", cost=Decimal(spent)
        )
        await s.commit()
        return project.id


async def test_a_project_ceiling_gate_resumes_only_once_its_owner_has_raised_it(client: AsyncClient) -> None:
    """Fix wave B1 (ruling FW-space-accept): an accept dispatches the parked wave without a
    re-check, so a Project-scope Resume let any editor spend one more wave past the owner's monthly
    cap per click. While the Project is still over, the accept is refused and resolves nothing."""
    task_id, owner, editor, _, _ = await _shared("project")
    project_id = await _project_spent(task_id)
    async with row_scope(Task, task_id) as s:
        gate = await Gate.open_budget_for(s, task_id)
        assert gate is not None
        gate_id = gate.id

    r = await client.post(f"/v1/gates/{gate_id}/resolve", json={"action": "accept"}, headers=_h(editor))
    assert r.status_code == 409, r.text
    error = r.json()["error"]
    assert error["code"] == "conflict"
    assert error["message"] == (
        "The Project's monthly budget is still spent; its owner can raise it in Project settings"
    )
    async with row_scope(Task, task_id) as s:
        still = await s.get(Gate, gate_id)
        assert still is not None and still.is_open()
        assert not [e for e in await TaskEvent.replay(s, task_id) if e.type == "gate.resolved"]

    r = await client.patch(f"/v1/projects/{project_id}", json={"budget_usd": "20"}, headers=_h(owner))
    assert r.status_code == 200, r.text
    r = await client.post(f"/v1/gates/{gate_id}/resolve", json={"action": "accept"}, headers=_h(editor))
    assert r.status_code == 200, r.text
    assert r.json()["action"] == "accept"


async def test_a_task_ceiling_gate_still_accepts_without_a_raise(client: AsyncClient) -> None:
    """Fix wave B1: task scope is unchanged - an editor who accepts without raising consents to one
    more wave (the dock raises first)."""
    task_id, _, editor, _, _ = await _shared("task")
    async with row_scope(Task, task_id) as s:
        gate = await Gate.open_budget_for(s, task_id)
        assert gate is not None
        gate_id = gate.id
    r = await client.post(f"/v1/gates/{gate_id}/resolve", json={"action": "accept"}, headers=_h(editor))
    assert r.status_code == 200 and r.json()["action"] == "accept"


async def test_a_raise_never_names_the_cost_confirmation_to_resume(client: AsyncClient) -> None:
    """Fix wave B4: `--resume` accepts the gate the raise names, and accepting the long-form cost
    confirmation is consent to the whole document (and its figure fleet) - not a raise's to give.
    Keyed on the title, so a "Budget reached" parked before `budget_scope` existed still resumes."""
    async with session_scope() as s:
        ada, workspace = await member(s, "Ada")
        task = await task_ops.create_task(s, ada, goal="a long document")
        task.status = TaskStatus.RUNNING.value
        await gate_ops._open_budget_gate(  # pyright: ignore[reportPrivateUsage]
            s, task, title=gate_ops.CONFIRM_TITLE, reason="consent"
        )
        key, raw = ApiKey.issue(ada.id, "t", workspace_id=workspace.id)
        s.add(key)
        await s.commit()
    r = await client.put(f"/v1/tasks/{task.id}/budget", json={"usd": "5"}, headers=_h(raw))
    assert r.status_code == 200 and r.json()["gate_id"] is None


async def test_a_legacy_budget_gate_with_no_scope_is_still_resumable(client: AsyncClient) -> None:
    async with session_scope() as s:
        ada, workspace = await member(s, "Ada")
        task = await task_ops.create_task(s, ada, goal="capped")
        task.status = TaskStatus.RUNNING.value
        gate = await gate_ops._open_budget_gate(  # pyright: ignore[reportPrivateUsage]
            s, task, title="Budget reached", reason="the task budget of $1 is spent"
        )
        key, raw = ApiKey.issue(ada.id, "t", workspace_id=workspace.id)
        s.add(key)
        await s.commit()
    r = await client.put(f"/v1/tasks/{task.id}/budget", json={"usd": "5"}, headers=_h(raw))
    assert r.status_code == 200 and r.json()["gate_id"] == str(gate.id)


@pytest.mark.parametrize(("typed", "stored"), [("1e2", "100.00"), ("2.000", "2.00"), ("2.5", "2.50")])
async def test_the_budget_is_stored_in_dollars_and_cents(
    client: AsyncClient, typed: str, stored: str
) -> None:
    """Fix wave B4: "1e2" was stored as "1E+2" - the row, the event, the response and the Bursar's
    reason now all say dollars and cents."""
    task_id, owner, _, _, _ = await _shared()
    r = await client.put(f"/v1/tasks/{task_id}/budget", json={"usd": typed}, headers=_h(owner))
    assert r.status_code == 200, r.text
    assert r.json()["usd"] == stored
    async with row_scope(Task, task_id) as s:
        task = await s.get(Task, task_id)
        assert task is not None and task.budget is not None and task.budget["usd"] == stored
        [event] = [e for e in await TaskEvent.replay(s, task_id) if e.type == "task.budget_set"]
        assert str(event.usd) == stored


async def test_a_task_ceiling_gate_does_not_resume_past_a_spent_project(client: AsyncClient) -> None:
    """Fix wave B1 amended (controller ruling on concern a): the Bursar names the TASK ceiling first,
    so a task over both ceilings parks on a task-scope gate - and its accept ran one more wave past
    the Project owner's cap too. Any "Budget reached" accept on a Project task is refused while the
    Project is over; once its owner raises it, the same accept resumes."""
    task_id, owner, editor, _, _ = await _shared("task")
    project_id = await _project_spent(task_id)
    async with row_scope(Task, task_id) as s:
        gate = await Gate.open_budget_for(s, task_id)
        assert gate is not None and GateSpec.model_validate(gate.spec).budget_scope == "task"
        gate_id = gate.id

    r = await client.post(f"/v1/gates/{gate_id}/resolve", json={"action": "accept"}, headers=_h(editor))
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "conflict"
    assert r.json()["error"]["message"] == gate_ops.PROJECT_STILL_SPENT
    async with row_scope(Task, task_id) as s:
        still = await s.get(Gate, gate_id)
        assert still is not None and still.is_open()

    r = await client.patch(f"/v1/projects/{project_id}", json={"budget_usd": "20"}, headers=_h(owner))
    assert r.status_code == 200, r.text
    r = await client.post(f"/v1/gates/{gate_id}/resolve", json={"action": "accept"}, headers=_h(editor))
    assert r.status_code == 200 and r.json()["action"] == "accept"


def _wave_over(
    task_limit: str | None, task_spent: str, projection: str, project: tuple[str, str] | None = None
) -> BudgetVerdict:
    """The verdict the wave projection parks on: the spend is under the ceiling, the wave is not."""
    verdict = over(
        task_limit=Decimal(task_limit) if task_limit else None,
        task_spent=Decimal(task_spent),
        project_limit=Decimal(project[0]) if project else None,
        project_spent=Decimal(project[1]) if project else Decimal(0),
        projection=Decimal(projection),
    )
    assert verdict is not None
    return verdict


@pytest.mark.parametrize(
    ("scope", "budget", "verdict"),
    [
        # The Project's own projection: $95 of $100 spent, a wave of about $10.
        ("project", "50.00", _wave_over(None, "0", "10.00", project=("100", "95"))),
        # A task ceiling the wave hit first, in a Project the same wave would carry past its ceiling.
        ("task", "10.00", _wave_over("10.00", "4.00", "8.00")),
    ],
)
async def test_a_wave_that_would_carry_the_project_past_its_ceiling_waits_for_its_owner(
    client: AsyncClient, scope: str, budget: str, verdict: BudgetVerdict
) -> None:
    """An accept dispatches the parked wave, so a Project whose spend alone is under its ceiling
    is still overrun by the wave the gate projected: $95 + $10 runs to $105 on a $100 Project. While
    the wave does not fit, no editor's accept resumes it; once the owner raises the Project, it does."""
    task_id, owner, editor, _, _ = await _shared(budget=budget, verdict=verdict)
    project_id = await _project_spent(task_id, budget="100", spent="95")
    async with row_scope(Task, task_id) as s:
        gate = await Gate.open_budget_for(s, task_id)
        assert gate is not None and GateSpec.model_validate(gate.spec).budget_scope == scope
        gate_id = gate.id

    r = await client.post(f"/v1/gates/{gate_id}/resolve", json={"action": "accept"}, headers=_h(editor))
    assert r.status_code == 409, r.text
    assert r.json()["error"]["message"] == gate_ops.PROJECT_WAVE_TOO_BIG
    async with row_scope(Task, task_id) as s:
        still = await s.get(Gate, gate_id)
        assert still is not None and still.is_open()

    r = await client.patch(f"/v1/projects/{project_id}", json={"budget_usd": "120"}, headers=_h(owner))
    assert r.status_code == 200, r.text
    r = await client.post(f"/v1/gates/{gate_id}/resolve", json={"action": "accept"}, headers=_h(editor))
    assert r.status_code == 200 and r.json()["action"] == "accept"


async def test_a_projection_gate_says_what_is_spent_and_what_the_wave_needs(client: AsyncClient) -> None:
    """A $10 ceiling, $4 spent, a wave of about $8: the gate says so, in words and in figures - on
    its spec, its event and the API - so the dock can offer a raise that clears the wave."""
    task_id, owner, _, _, _ = await _shared(budget="10.00", verdict=_wave_over("10.00", "4.00", "8.00"))
    figures = BudgetFigures(
        ceiling_usd=Decimal("10.00"), spent_usd=Decimal("4.00"), projected_usd=Decimal("8.00")
    )
    reason = (
        "$4.00 spent; the next wave needs about $8.00 against the task budget of $10.00; "
        "raise it and resume, or cancel"
    )
    async with row_scope(Task, task_id) as s:
        gate = await Gate.open_budget_for(s, task_id)
        assert gate is not None
        spec = GateSpec.model_validate(gate.spec)
        assert (spec.reason, spec.budget_figures) == (reason, figures)
        [opened] = [e for e in await TaskEvent.replay(s, task_id) if e.type == "gate.opened"]
        assert (opened.reason, opened.budget_figures) == (reason, figures)
    r = await client.get("/v1/gates", headers=_h(owner))
    assert r.status_code == 200, r.text
    [listed] = [g for g in r.json() if g["task_id"] == str(task_id)]
    assert listed["budget_figures"] == {
        "ceiling_usd": "10.00",
        "spent_usd": "4.00",
        "projected_usd": "8.00",
        "exhausted": False,  # a ceiling, not the workspace's allowance (ADR-103)
    }


async def test_a_gate_the_spend_alone_opened_projects_nothing() -> None:
    task_id, _, _, _, _ = await _shared()
    async with row_scope(Task, task_id) as s:
        gate = await Gate.open_budget_for(s, task_id)
        assert gate is not None
        figures = GateSpec.model_validate(gate.spec).budget_figures
        assert figures == BudgetFigures(
            ceiling_usd=Decimal("0.01"), spent_usd=Decimal("0.02"), projected_usd=Decimal(0)
        )


async def test_a_raise_below_the_ceiling_is_refused_while_budget_reached_is_open(client: AsyncClient) -> None:
    """Resuming a projection gate with $5 typed on a $10 ceiling would LOWER the budget under a
    "Raise" label and run the wave past it: while "Budget reached" is open, a raise is at least
    the ceiling, and a refused one writes nothing."""
    task_id, _, editor, _, _ = await _shared(budget="10.00", verdict=_wave_over("10.00", "4.00", "8.00"))
    r = await client.put(f"/v1/tasks/{task_id}/budget", json={"usd": "5"}, headers=_h(editor))
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "validation_failed"
    assert "$10.00" in r.json()["error"]["message"]
    async with row_scope(Task, task_id) as s:
        task = await s.get(Task, task_id)
        assert task is not None and task.budget == {"usd": "10.00"}
        assert not [e for e in await TaskEvent.replay(s, task_id) if e.type == "task.budget_set"]

    for usd in ("10", "13"):
        r = await client.put(f"/v1/tasks/{task_id}/budget", json={"usd": usd}, headers=_h(editor))
        assert r.status_code == 200, r.text


async def test_with_no_budget_gate_open_the_budget_can_be_lowered(client: AsyncClient) -> None:
    async with session_scope() as s:
        ada, workspace = await member(s, "Ada")
        task = await task_ops.create_task(s, ada, goal="capped")
        task.budget = {"usd": "10.00"}
        key, raw = ApiKey.issue(ada.id, "t", workspace_id=workspace.id)
        s.add(key)
        await s.commit()
    r = await client.put(f"/v1/tasks/{task.id}/budget", json={"usd": "5"}, headers=_h(raw))
    assert r.status_code == 200 and r.json()["usd"] == "5.00" and r.json()["previous_usd"] == "10.00"


async def _create(budget: dict[str, object] | None = None) -> tuple[Actor, Task]:
    async with session_scope() as s:
        result = await auth_ops.signup(
            s, email=f"d-{uuid.uuid4().hex[:8]}@example.com", name="Ada", password="a-long-enough-password"
        )
        actor = await s.get(Actor, result.user.actor_id)
        assert actor is not None
        task = await task_ops.create_task(s, actor, goal="uncapped", budget=budget)
        await s.commit()
        return actor, task


async def test_a_task_created_without_a_budget_gets_the_default_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.workspaces, "task_default_budget_usd", Decimal("2"))
    _, task = await _create()
    assert task.budget == {"usd": "2.00", "source": "default"}, "marked: nobody chose it"


async def test_an_explicit_budget_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.workspaces, "task_default_budget_usd", Decimal("2"))
    _, task = await _create({"usd": "7.50"})
    assert task.budget == {"usd": "7.50"}


async def test_unset_default_leaves_no_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.workspaces, "task_default_budget_usd", None)
    _, task = await _create()
    assert task.budget is None


async def test_a_follow_up_keeps_the_tasks_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.workspaces, "task_default_budget_usd", Decimal("2"))
    actor, task = await _create({"usd": "7.50"})
    monkeypatch.setattr(settings.workspaces, "task_default_budget_usd", Decimal("3"))
    async with row_scope(Task, task.id) as s:
        await s.execute(update(Task).where(Task.id == task.id).values(status="delivered"))
        again = await task_ops.continue_task(s, actor, task.id, goal="and more")
        assert again.budget == {"usd": "7.50"}


# ── the long-form cost confirmation and a defaulted cap: one question, never two ────────────


async def _long_document(
    monkeypatch: pytest.MonkeyPatch, budget: dict[str, object] | None, *, default: str = "0.01"
) -> tuple[Actor, Task]:
    """A long document past its outline, three sections ready to write, whose estimate passes the
    confirmation threshold; created with `budget` under a `default` cap."""
    monkeypatch.setattr(settings.workspaces, "task_default_budget_usd", Decimal(default))
    monkeypatch.setattr(settings.worker, "longform_confirm_usd", 0.0)
    actor, task = await _create(budget)
    async with row_scope(Task, task.id) as s:
        row = await s.get(Task, task.id)
        assert row is not None
        row.deliverable, row.scale = ArtifactType.DOCUMENT.value, SizeBucket.LONG.value
        row.status = TaskStatus.RUNNING.value
        for n in range(3):
            title = f"section {n + 1}"
            spec = StepSpec(
                kind=StepKind.STEP,
                agent=AgentName("section_writer"),
                title=title,
                contract=Contract(goal=title),
                lane_role="section",
            )
            s.add(
                PlanStep(
                    id=uuid.uuid4(),
                    task_id=task.id,
                    ordinal=n,
                    kind=StepKind.STEP.value,
                    state=StepState.READY.value,
                    title=title,
                    spec=spec.model_dump(mode="json"),
                    attempt=0,
                )
            )
        await s.commit()
    return actor, task


async def _boundary(task_id: uuid.UUID) -> Gate | None:
    """The wave boundary's check, and the gate it parked on."""
    async with row_scope(Task, task_id) as s:
        gate_id = await gate_ops.check_budget(s, task_id)
        if gate_id is None:
            return None
        gate = await s.get(Gate, gate_id)
        s.expunge_all()
        return gate


async def test_a_defaulted_cap_asks_once_and_the_accepted_estimate_runs(
    recording_dispatcher: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The person never chose the default cap, so it is not their consent: the confirmation asks.
    Accepting it consents to the estimate, which the cap rises to, so the boundary that follows runs
    the sections instead of asking again with "Budget reached"."""
    actor, task = await _long_document(monkeypatch, None)

    asked = await _boundary(task.id)
    assert asked is not None and GateSpec.model_validate(asked.spec).title == gate_ops.CONFIRM_TITLE

    async with row_scope(Task, task.id) as s:
        await gate_ops.resolve(s, actor, asked.id, GateAction.ACCEPT)
    assert await effects.resume_from_budget(str(task.id), task.turn) == "running"  # the turn's resume
    assert await _boundary(task.id) is None, "no second question"
    async with row_scope(Task, task.id) as s:
        row = await s.get(Task, task.id)
        assert row is not None and row.budget is not None and "source" not in row.budget
        assert Decimal(str(row.budget["usd"])) > Decimal("0.01"), "the cap rose to the estimate"
        assert await Gate.open_budget_for(s, task.id) is None
        raised = [e for e in await TaskEvent.replay(s, task.id) if isinstance(e, TaskBudgetSet)]
        assert [(e.previous_usd, e.actor.id if e.actor else None) for e in raised] == [
            (Decimal("0.01"), actor.id)
        ]


async def test_accepting_never_lowers_a_defaulted_cap_that_already_covers_the_estimate(
    recording_dispatcher: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    actor, task = await _long_document(monkeypatch, None, default="500")
    asked = await _boundary(task.id)
    assert asked is not None, "a default nobody chose still asks"

    async with row_scope(Task, task.id) as s:
        await gate_ops.resolve(s, actor, asked.id, GateAction.ACCEPT)
        row = await s.get(Task, task.id)
        assert row is not None and row.budget == {"usd": "500.00"}, "kept, and now the person's consent"


@pytest.mark.parametrize("how", ["at create", "set by the person"])
async def test_a_cap_a_person_chose_that_covers_the_estimate_is_the_consent(
    how: str, recording_dispatcher: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    actor, task = await _long_document(monkeypatch, {"usd": "500.00"} if how == "at create" else None)
    if how == "set by the person":
        async with row_scope(Task, task.id) as s:
            await steering.set_budget(s, actor, task.id, Decimal("500"))

    assert await _boundary(task.id) is None, "no confirmation: the person's cap already consents"
