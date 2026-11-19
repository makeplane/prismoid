# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Resolving a browser approve gate by scope (task 7): the creator's `always`
writes a standing allowance; the project's editor+ may accept `once`/`task` but never
`always`; and a connector-source gate keeps the creator-only rule untouched."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models.actor import Actor
from prismoid.db.models.gate import Gate
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.project import Project, Role
from prismoid.db.models.project_member import ProjectMember
from prismoid.db.models.task import Task
from prismoid.db.session import session_scope
from prismoid.errors import Forbidden, ValidationFailed
from prismoid.operations import egress_allowances as allow_ops
from prismoid.operations import gates
from prismoid.operations import tasks as task_ops
from prismoid.schemas.ids import StepId
from prismoid.schemas.plan import GateAction
from tests.support.cast import member

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("silent_dispatcher")]

ITEMS = [("call-1", "sso.vendor.com", "browser.open", "open sso.vendor.com/login")]


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    async with session_scope() as session:
        yield session


@pytest.fixture
async def actor(db: AsyncSession) -> Actor:
    row, _ = await member(db, "Creator")
    await db.commit()
    return row


@pytest.fixture
async def editor(db: AsyncSession) -> Actor:
    row, _ = await member(db, "Editor")
    await db.commit()
    return row


async def _step_for(db: AsyncSession, task: Task) -> PlanStep:
    row = await db.get(Task, task.id)
    assert row is not None
    step = PlanStep(task_id=row.id, ordinal=0, title="open the site")
    db.add(step)
    await db.flush()
    return step


@pytest.fixture
async def project_task(db: AsyncSession, actor: Actor, editor: Actor) -> Task:
    """A project the creator owns, the `editor` actor as its EDITOR, and a task the creator
    made in it - the fixture `test_connector_attach.py`'s `_shared_project` sets up inline."""
    project = Project(name="shared", owner_actor_id=actor.id)
    db.add(project)
    await db.commit()
    db.add(ProjectMember(project_id=project.id, actor_id=editor.id, role=Role.EDITOR, added_by=actor.id))
    await db.commit()
    task = await task_ops.create_task(db, actor, goal="update the records", project_id=project.id)
    row = await db.get(Task, task.id)
    assert row is not None
    return row


async def test_resolving_a_browser_gate_by_scope(db: AsyncSession, actor: Actor) -> None:
    task = await task_ops.create_task(db, actor, goal="update the records")
    row = await db.get(Task, task.id)
    assert row is not None
    step = await _step_for(db, row)
    gate = await gates.open_approval(
        db,
        row,
        StepId(step.id),
        ITEMS,
        source="browser",
        host="sso.vendor.com",
        scopes=("once", "task", "always"),
    )
    await db.commit()

    with pytest.raises(ValidationFailed):
        await gates.resolve(db, actor, gate.id, GateAction.ACCEPT, {"scope": "forever"})

    resolution = await gates.resolve(db, actor, gate.id, GateAction.ACCEPT, {"scope": "always"})
    assert resolution.content == {"scope": "always"}
    assert await allow_ops.hosts_for_owner(db, actor.id) == ["sso.vendor.com"]
    rows = await allow_ops.list_allowances(db, actor)
    assert rows[0].source == "gate" and rows[0].gate_id == gate.id


async def test_always_refuses_a_bad_host_defence_in_depth(db: AsyncSession, actor: Actor) -> None:
    """`resolve()` runs `check_host` before ever turning a
    gate's stored `spec.host` into a standing allowance - a gate's spec is stored data, and
    this is the one place it becomes a credential-shaped grant. The browser tool only ever
    offers a host `check_host` would itself accept, so this is belt-and-braces, not a real
    path - proven here with a host it would refuse outright (loopback)."""
    task = await task_ops.create_task(db, actor, goal="update the records")
    row = await db.get(Task, task.id)
    assert row is not None
    step = await _step_for(db, row)
    gate = await gates.open_approval(
        db, row, StepId(step.id), ITEMS, source="browser", host="localhost", scopes=("once", "task", "always")
    )
    await db.commit()

    with pytest.raises(ValidationFailed) as excinfo:
        await gates.resolve(db, actor, gate.id, GateAction.ACCEPT, {"scope": "always"})
    assert "host" in excinfo.value.fields
    assert await allow_ops.hosts_for_owner(db, actor.id) == []


async def test_a_browser_danger_ask_has_no_scopes_and_needs_no_content(
    db: AsyncSession, actor: Actor
) -> None:
    """`scopes=()` is the danger-ask shape (the confirm, never an allowlist) - the
    plain accept/deny wire, unchanged."""
    task = await task_ops.create_task(db, actor, goal="click the risky thing")
    row = await db.get(Task, task.id)
    assert row is not None
    step = await _step_for(db, row)
    gate = await gates.open_approval(
        db, row, StepId(step.id), ITEMS, source="browser", host="sso.vendor.com", scopes=()
    )
    await db.commit()

    resolution = await gates.resolve(db, actor, gate.id, GateAction.ACCEPT, None)
    assert resolution.content == {}
    assert await allow_ops.hosts_for_owner(db, actor.id) == []


async def test_an_editor_may_accept_once_or_task_but_never_always(
    db: AsyncSession, actor: Actor, editor: Actor, project_task: Task
) -> None:
    step = await _step_for(db, project_task)
    gate = await gates.open_approval(
        db,
        project_task,
        StepId(step.id),
        ITEMS,
        source="browser",
        host="sso.vendor.com",
        scopes=("once", "task", "always"),
    )
    await db.commit()

    with pytest.raises(Forbidden):
        await gates.resolve(db, editor, gate.id, GateAction.ACCEPT, {"scope": "always"})

    resolution = await gates.resolve(db, editor, gate.id, GateAction.ACCEPT, {"scope": "task"})
    assert resolution.content == {"scope": "task"}
    assert await allow_ops.hosts_for_owner(db, project_task.creator_actor_id) == []


async def test_an_accept_that_names_no_scope_takes_the_gates_default(
    db: AsyncSession, actor: Actor, editor: Actor, project_task: Task
) -> None:
    """The default is the server's, decided once for every client: `task` when offered, else
    the gate's first offer. A Project editor's CLI cannot look it up - the gate list it could read
    it from is the creator's own queue - and its bare `--accept` used to come back 422."""
    step = await _step_for(db, project_task)

    async def browser_gate(scopes: tuple[str, ...]) -> Gate:
        gate = await gates.open_approval(
            db, project_task, StepId(step.id), ITEMS, source="browser", host="sso.vendor.com", scopes=scopes
        )
        await db.commit()
        return gate

    by_editor = await browser_gate(("once", "task", "always"))
    resolution = await gates.resolve(db, editor, by_editor.id, GateAction.ACCEPT, None)
    assert resolution.content == {"scope": "task"}
    no_task = await browser_gate(("once", "always"))
    resolution = await gates.resolve(db, actor, no_task.id, GateAction.ACCEPT, {})
    assert resolution.content == {"scope": "once"}
    assert await allow_ops.hosts_for_owner(db, project_task.creator_actor_id) == []


async def test_a_connector_gate_stays_creator_only(
    db: AsyncSession, actor: Actor, editor: Actor, project_task: Task
) -> None:
    step = await _step_for(db, project_task)
    gate = await gates.open_approval(
        db, project_task, StepId(step.id), [("c", "Gmail", "gmail_send", "to: a@b.c")]
    )
    await db.commit()

    with pytest.raises(Forbidden):
        await gates.resolve(db, editor, gate.id, GateAction.ACCEPT, None)

    resolution = await gates.resolve(db, actor, gate.id, GateAction.ACCEPT, None)
    assert resolution.content == {}


async def test_a_mixed_batch_gate_resolves_as_connector_creator_only(
    db: AsyncSession, actor: Actor, editor: Actor, project_task: Task
) -> None:
    """A batch mixing a browser call with a connector call
    is classified `source="connector"` by the ENGINE before `open_approval` is ever called
    (proved directly in `tests/unit/operations/execution/test_approval_deferral_wiring.py`'s
    `test_a_mixed_batch_is_a_connector_gate_creator_only`) - this proves `resolve()` then
    treats such a gate exactly like today's plain connector gate: creator-only, `Forbidden`
    for the editor, even though a browser call rode along in the same batch. Accept must
    approve every call in the batch, so a browser-looking gate must never let a non-creator
    wave a connector call through."""
    step = await _step_for(db, project_task)
    gate = await gates.open_approval(
        db,
        project_task,
        StepId(step.id),
        [
            ("c1", "sso.vendor.com", "browser.open", "open sso.vendor.com/login"),
            ("c2", "Gmail", "gmail_send", "to: a@b.c"),
        ],
        source="connector",  # what the engine computes for a mixed batch
    )
    await db.commit()

    with pytest.raises(Forbidden):
        await gates.resolve(db, editor, gate.id, GateAction.ACCEPT, None)

    resolution = await gates.resolve(db, actor, gate.id, GateAction.ACCEPT, None)
    assert resolution.content == {}


async def test_an_open_gate_reads_by_id_as_its_reader_may_answer_it(
    db: AsyncSession, actor: Actor, editor: Actor, project_task: Task
) -> None:
    """`GET /v1/gates/{id}`: the CLI's answer path for a Project editor (their own queue holds only
    their tasks' gates). The creator is offered `always`, the editor never; a stranger, and
    anyone once the gate is answered, reads 404."""
    from prismoid.api.endpoints.gates import get_gate
    from prismoid.errors import NotFound

    step = await _step_for(db, project_task)
    gate = await gates.open_approval(
        db,
        project_task,
        StepId(step.id),
        ITEMS,
        source="browser",
        host="sso.vendor.com",
        scopes=("once", "task", "always"),
    )
    await db.commit()

    assert (await get_gate(gate.id, actor, db)).scopes == ["once", "task", "always"]
    assert (await get_gate(gate.id, editor, db)).scopes == ["once", "task"]
    stranger, _ = await member(db, "Stranger")
    with pytest.raises(NotFound):
        await get_gate(gate.id, stranger, db)

    await gates.resolve(db, editor, gate.id, GateAction.ACCEPT, {"scope": "task"})
    with pytest.raises(NotFound):
        await get_gate(gate.id, editor, db)
