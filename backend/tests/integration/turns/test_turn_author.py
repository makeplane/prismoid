# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The turn's author labels spend, gets the knocks, wins races by name."""

import datetime as dt
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.api.serializers.tasks import task_summary
from prismoid.db.base import utcnow
from prismoid.db.models.actor import Actor
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.inbox import InboxItem, InboxKind
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.project import Project
from prismoid.db.models.project_member import ProjectMember
from prismoid.db.models.task import Task
from prismoid.db.models.usage import UsageEvent
from prismoid.db.models.user import User
from prismoid.db.models.workspace import WorkspaceRole
from prismoid.db.session import session_scope
from prismoid.errors import Conflict
from prismoid.operations import gates, inbox, steering
from prismoid.operations import projects as project_ops
from prismoid.operations import tasks as task_ops
from prismoid.operations import usage as usage_ops
from prismoid.schemas.events import TaskContinued
from prismoid.schemas.ids import StepId, TaskId
from prismoid.schemas.model_class import Family
from prismoid.schemas.plan import TaskStatus
from prismoid.schemas.usage import Unit, UsageRecord
from prismoid.schemas.verification import Diagnosis
from tests.support.cast import member

pytestmark = pytest.mark.integration


async def _trio(s: AsyncSession) -> tuple[Actor, Actor, Actor, Project, Task]:
    """Ada owns a Project; Bob edits; Cy views - all in one workspace."""
    ada, workspace = await member(s, "Ada")
    bob, _ = await member(s, "Bob", role=WorkspaceRole.MEMBER, workspace=workspace)
    cy, _ = await member(s, "Cy", role=WorkspaceRole.MEMBER, workspace=workspace)
    project = (await project_ops.create(s, ada, name=f"launch-{uuid.uuid4().hex[:6]}")).project
    s.add(ProjectMember(project_id=project.id, actor_id=bob.id, role="editor", added_by=ada.id))
    s.add(ProjectMember(project_id=project.id, actor_id=cy.id, role="viewer", added_by=ada.id))
    await s.flush()
    task = await task_ops.create_task(s, ada, goal="compare three trackers", project_id=project.id)
    return ada, bob, cy, project, task


async def _settle(s: AsyncSession, task: Task) -> None:
    task.transition(TaskStatus.PLANNING)
    task.transition(TaskStatus.RUNNING)
    task.transition(TaskStatus.DELIVERED)
    await s.commit()


def _spend(task_id: uuid.UUID, cost: str, *, at: dt.datetime | None = None) -> UsageRecord:
    return UsageRecord(
        task_id=TaskId(task_id),
        provider="local",
        model="m",
        family=Family("qwen"),
        units={Unit.REQUEST: 1},
        cost_usd=Decimal(cost),
        price_version=1,
        at=at or utcnow(),
    )


async def _humans(s: AsyncSession, *people: Actor) -> None:
    """The admin rollups name people through their User row."""
    for p in people:
        s.add(
            User(
                actor_id=p.id,
                email=f"{p.display_name.lower()}{uuid.uuid4().hex[:8]}@example.com",
                name=p.display_name,
            )
        )
    await s.flush()


async def _label(s: AsyncSession, row: UsageEvent) -> uuid.UUID | None:
    return await s.scalar(select(UsageEvent.actor_id).where(UsageEvent.id == row.id))


async def test_the_turn_author_labels_spend(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        ada, bob, _cy, _project, task = await _trio(s)
        assert task.turn_actor_id == ada.id
        await _settle(s, task)
        ada_row = await usage_ops.record(s, _spend(task.id, "0.25"))
        assert await _label(s, ada_row) == ada.id

        await task_ops.continue_task(s, bob, task.id, goal="add pricing")
        assert task.turn_actor_id == bob.id

        bob_row = await usage_ops.record(s, _spend(task.id, "0.75"))
        await s.flush()
        assert await _label(s, bob_row) == bob.id
        # The label is a report, never a wallet: Bob's turn is Bob's, Ada's turn stays Ada's.
        assert await UsageEvent.total_for_creator(s, bob.id) == Decimal("0.75")
        assert await UsageEvent.total_for_creator(s, ada.id) == Decimal("0.25")


async def test_history_without_a_label_still_means_the_creator(silent_dispatcher: None) -> None:
    """Rows written before turn authors existed carry no actor_id; every rollup reads them as the
    creator's."""
    async with session_scope() as s:
        ada, bob, _cy, _project, task = await _trio(s)
        await _humans(s, ada, bob)
        # The ledger is append-only, so the legacy row is INSERTed unlabeled, not updated to be.
        s.add(
            UsageEvent(
                id=uuid.uuid4(),
                task_id=task.id,
                workspace_id=UsageEvent.parent_workspace(task.id, None),
                provider="local",
                model="m",
                family="qwen",
                units={"request": 1},
                cost_usd=Decimal("0.40"),
                at=utcnow(),
            )
        )
        await s.flush()
        assert await UsageEvent.total_for_creator(s, ada.id) == Decimal("0.40")
        assert await UsageEvent.total_for_creator(s, bob.id) == Decimal("0")

        since = utcnow().replace(year=2000)
        spenders = await UsageEvent.top_spenders_since(s, since, limit=100_000)
        assert (ada.id, Decimal("0.40")) in [(uid, cost) for uid, _e, _n, cost, _k in spenders]
        series = await UsageEvent.daily_series(s, since, creator_id=ada.id)
        assert sum(cost for _d, _k, cost, _n in series) == Decimal("0.40")


async def test_rollups_group_by_the_turn_author(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        ada, bob, _cy, _project, task = await _trio(s)
        await _humans(s, ada, bob)
        await _settle(s, task)
        await task_ops.continue_task(s, bob, task.id, goal="add pricing")
        await usage_ops.record(s, _spend(task.id, "0.60"))
        await s.flush()
        since = utcnow().replace(year=2000)
        spenders = {
            uid: cost
            for uid, _e, _n, cost, _k in await UsageEvent.top_spenders_since(s, since, limit=100_000)
        }
        assert spenders[bob.id] == Decimal("0.60")
        assert ada.id not in spenders
        by_bob = await UsageEvent.daily_series(s, since, creator_id=bob.id)
        assert sum(cost for _d, _k, cost, _n in by_bob) == Decimal("0.60")
        assert await UsageEvent.daily_series(s, since, creator_id=ada.id) == []


async def test_a_follow_up_that_loses_the_race_names_the_winner(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        ada, bob, _cy, _project, task = await _trio(s)
        await _settle(s, task)
        await task_ops.continue_task(s, bob, task.id, goal="add pricing")  # Bob won: turn 2 runs
        with pytest.raises(Conflict) as lost:
            await task_ops.continue_task(s, ada, task.id, goal="add a chart")
        assert "Bob just started the next turn" in str(lost.value)


async def test_the_recheck_under_the_lock_names_the_winner_too(
    silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A loser that passed the early guard while the task was settled, and lost while its slow
    side effects ran, still hears the winner's name from the re-check after the row lock."""
    async with session_scope() as s:
        ada, bob, _cy, _project, task = await _trio(s)
        await _settle(s, task)
        real = task_ops.store_inputs

        async def bob_wins_meanwhile(raw):  # pyright: ignore[reportMissingParameterType]
            task.turn_actor_id = bob.id
            task.turn += 1
            task.transition(TaskStatus.PLANNING)
            await s.flush()
            return await real(raw)

        monkeypatch.setattr(task_ops, "store_inputs", bob_wins_meanwhile)
        with pytest.raises(Conflict) as lost:
            await task_ops.continue_task(s, ada, task.id, goal="add a chart")
        assert "Bob just started the next turn" in str(lost.value)


async def test_a_live_task_with_no_author_names_the_creator(silent_dispatcher: None) -> None:
    """A NULL turn author reads as the creator, as everywhere else (the migration's promise)."""
    async with session_scope() as s:
        _ada, bob, _cy, _project, task = await _trio(s)
        task.turn_actor_id = None  # the author's actor row is gone (SET NULL)
        await s.flush()
        with pytest.raises(Conflict, match="Ada just started the next turn"):
            await task_ops.continue_task(s, bob, task.id, goal="add a chart")


async def test_a_budget_change_is_recorded(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        _ada, bob, _cy, _project, task = await _trio(s)
        task.budget = {"usd": "1"}
        await _settle(s, task)
        await task_ops.continue_task(s, bob, task.id, goal="add pricing", budget={"usd": "3"})
        newest = await TaskEvent.latest_of_type(s, task.id, "task.continued")
        assert isinstance(newest, TaskContinued)
        assert newest.budget_after == {"usd": "3"}
        assert newest.budget_before == {"usd": "1"}
        assert newest.actor is not None and newest.actor.name == "Bob"


async def test_a_follow_up_that_leaves_the_cap_alone_records_no_budget(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        _ada, bob, _cy, _project, task = await _trio(s)
        task.budget = {"usd": "1"}
        await _settle(s, task)
        await task_ops.continue_task(s, bob, task.id, goal="add pricing")
        newest = await TaskEvent.latest_of_type(s, task.id, "task.continued")
        assert isinstance(newest, TaskContinued)
        assert newest.budget_before is None and newest.budget_after is None


async def test_resending_the_same_cap_is_not_a_change(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        _ada, bob, _cy, _project, task = await _trio(s)
        task.budget = {"usd": "1"}
        await _settle(s, task)
        await task_ops.continue_task(s, bob, task.id, goal="add pricing", budget={"usd": "1"})
        newest = await TaskEvent.latest_of_type(s, task.id, "task.continued")
        assert isinstance(newest, TaskContinued)
        assert newest.budget_before is None and newest.budget_after is None


async def test_an_empty_cap_and_no_cap_stay_distinct(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        _ada, bob, _cy, _project, task = await _trio(s)
        task.budget = {"usd": "1"}
        await _settle(s, task)
        await task_ops.continue_task(s, bob, task.id, goal="drop the cap", budget={})
        newest = await TaskEvent.latest_of_type(s, task.id, "task.continued")
        assert isinstance(newest, TaskContinued)
        assert newest.budget_before == {"usd": "1"}
        assert newest.budget_after == {}, "an emptied cap is {}, never folded into None"


async def test_the_first_cap_on_an_uncapped_task_is_recorded(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        _ada, bob, _cy, _project, task = await _trio(s)
        assert task.budget is None
        await _settle(s, task)
        await task_ops.continue_task(s, bob, task.id, goal="cap it", budget={"usd": "2"})
        newest = await TaskEvent.latest_of_type(s, task.id, "task.continued")
        assert isinstance(newest, TaskContinued)
        assert newest.budget_before is None and newest.budget_after == {"usd": "2"}


async def test_a_steer_moves_the_spend_label_but_not_the_turn_author(silent_dispatcher: None) -> None:
    """Spend follows the epoch. Bob's steer opens the next one, so the spend after it
    is labelled Bob's; the turn is still Ada's (she gets its knocks, and its follow-up race)."""
    async with session_scope() as s:
        ada, bob, _cy, _project, task = await _trio(s)
        task.transition(TaskStatus.PLANNING)
        task.transition(TaskStatus.RUNNING)
        await s.commit()
        await steering.steer(s, bob, task.id, "focus on pricing")
        assert task.turn_actor_id == ada.id
        row = await usage_ops.record(s, _spend(task.id, "0.10"))
        await s.flush()
        assert await _label(s, row) == bob.id, "Bob steered Ada's turn: the spend after it is labelled Bob's"


async def test_total_for_creator_adds_labeled_rows_to_unlabeled_history(silent_dispatcher: None) -> None:
    """The two index-driven halves (labeled by author + unlabeled history by task creator), and
    the `since` window applying to both. Rows are dated now and now+2h - never before the test
    started, which is all the suite's teardown sweeps - with `since` between the two."""
    async with session_scope() as s:
        ada, bob, _cy, _project, task = await _trio(s)
        await _settle(s, task)
        early, late = utcnow(), utcnow() + dt.timedelta(hours=2)

        def legacy(cost: str, at: dt.datetime) -> UsageEvent:
            return UsageEvent(
                id=uuid.uuid4(),
                task_id=task.id,
                workspace_id=UsageEvent.parent_workspace(task.id, None),
                provider="local",
                model="m",
                family="qwen",
                units={"request": 1},
                cost_usd=Decimal(cost),
                at=at,
            )

        s.add_all([legacy("0.30", early), legacy("0.40", late)])  # Ada's, unlabeled
        await usage_ops.record(s, _spend(task.id, "0.20", at=early))  # labeled Ada
        await task_ops.continue_task(s, bob, task.id, goal="add pricing")
        await usage_ops.record(s, _spend(task.id, "0.50", at=late))  # labeled Bob, on Ada's task
        await s.flush()

        assert await UsageEvent.total_for_creator(s, ada.id) == Decimal("0.90")
        assert await UsageEvent.total_for_creator(s, bob.id) == Decimal("0.50")
        since = utcnow() + dt.timedelta(hours=1)
        assert await UsageEvent.total_for_creator(s, ada.id, since=since) == Decimal("0.40")
        assert await UsageEvent.total_for_creator(s, bob.id, since=since) == Decimal("0.50")


async def _rows(s: AsyncSession, task_id: uuid.UUID, kind: InboxKind) -> list[uuid.UUID]:
    rows = await s.scalars(
        select(InboxItem.actor_id).where(InboxItem.task_id == task_id, InboxItem.kind == kind.value)
    )
    return sorted(rows)


async def test_knocks_reach_the_creator_and_the_turn_author(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        ada, bob, _cy, project, task = await _trio(s)
        await _settle(s, task)
        await task_ops.continue_task(s, bob, task.id, goal="add pricing")
        first = await inbox.task_completed(s, task, "done")
        assert first.actor_id == ada.id  # the creator's row is the one returned
        assert await _rows(s, task.id, InboxKind.TASK_COMPLETED) == sorted([ada.id, bob.id])

        # Budget rides the same door: one each for the two of them.
        await inbox.budget_reached(s, task, "cap hit")
        assert await _rows(s, task.id, InboxKind.BUDGET) == sorted([ada.id, bob.id])

        # A task Ada authored alone knocks once.
        solo = await task_ops.create_task(s, ada, goal="a solo memo", project_id=project.id)
        await inbox.task_completed(s, solo, "done")
        assert await _rows(s, solo.id, InboxKind.TASK_COMPLETED) == [ada.id]


async def _bob_authors_turn_two(s: AsyncSession) -> tuple[Actor, Actor, Task, PlanStep]:
    ada, bob, _cy, _project, task = await _trio(s)
    step = PlanStep(task_id=task.id, ordinal=0, title="send it")
    s.add(step)
    await s.flush()
    await _settle(s, task)
    await task_ops.continue_task(s, bob, task.id, goal="add pricing")
    assert task.turn_actor_id == bob.id
    return ada, bob, task, step


_ITEMS = [("call_0", "Gmail", "gmail_send_email", "to: a@b.c")]


async def test_a_connector_approval_knocks_only_the_creator(silent_dispatcher: None) -> None:
    """Only the creator can accept it (it runs on THEIR credential): the author is not told
    about a button they cannot press."""
    async with session_scope() as s:
        ada, _bob, task, step = await _bob_authors_turn_two(s)
        gate = await gates.open_approval(s, task, StepId(step.id), _ITEMS)
        await s.flush()
        held = await s.scalars(select(InboxItem.actor_id).where(InboxItem.gate_id == gate.id))
        assert list(held) == [ada.id]


async def test_a_connect_request_knocks_only_the_creator(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        ada, _bob, task, step = await _bob_authors_turn_two(s)
        gate = await gates.open_connect(
            s, task, StepId(step.id), slug="notion", display="Notion", why="To create the page there."
        )
        await s.flush()
        held = await s.scalars(select(InboxItem.actor_id).where(InboxItem.gate_id == gate.id))
        assert list(held) == [ada.id]


async def test_a_browser_approval_knocks_the_creator_and_the_turn_author(silent_dispatcher: None) -> None:
    """Any editor may accept a browser ask, so the turn's author hears about it too."""
    async with session_scope() as s:
        ada, bob, task, step = await _bob_authors_turn_two(s)
        gate = await gates.open_approval(
            s,
            task,
            StepId(step.id),
            [("call_0", "Browser", "browser_open", "https://example.com")],
            source="browser",
            host="example.com",
            scopes=("once", "always"),
        )
        await s.flush()
        held = await s.scalars(select(InboxItem.actor_id).where(InboxItem.gate_id == gate.id))
        assert sorted(held) == sorted([ada.id, bob.id])


async def test_an_escalation_reaches_the_creator_and_the_turn_author(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        ada, bob, task, _step = await _bob_authors_turn_two(s)
        first = await inbox.escalated(
            s,
            task,
            Diagnosis(what_failed="the draft", why="no sources", attempts=3, suggested_next=["retry"]),
        )
        assert first.actor_id == ada.id
        assert await _rows(s, task.id, InboxKind.ESCALATION) == sorted([ada.id, bob.id])


async def _row(s: AsyncSession, viewer: Actor, project: Project):
    """The one task in the Project, as the list serves it: the row with its two names."""
    tasks, names, _ = await task_ops.list_tasks(s, viewer, project_id=project.id)
    (row,) = [task_summary(t, names) for t in tasks]
    return row


async def test_the_list_names_the_asker_and_whose_turn_it_is(silent_dispatcher: None) -> None:
    """A row says who asked and whose turn is running, and
    Bob's follow-up moves the second name without touching the first."""
    async with session_scope() as s:
        ada, bob, _cy, project, task = await _trio(s)
        await _humans(s, ada, bob)

        first = await _row(s, bob, project)
        assert (first.creator_name, first.turn_author_name) == ("Ada", "Ada")

        await _settle(s, task)
        await task_ops.continue_task(s, bob, task.id, goal="add pricing")
        after = await _row(s, ada, project)
        assert (after.creator_name, after.turn_author_name) == ("Ada", "Bob")
        # the ids ride with the names: two people can share a name, never an id
        assert (after.creator_id, after.turn_author_id) == (ada.id, bob.id)


async def test_an_unknown_person_is_null_and_a_null_turn_reads_as_the_creator(
    silent_dispatcher: None,
) -> None:
    """A name we cannot resolve stays null, never a guess; and a row from before turn authors (no turn
    author) reads as its creator's turn, as everywhere else."""
    async with session_scope() as s:
        ada, bob, _cy, project, task = await _trio(s)
        await _humans(s, ada)  # Bob has an Actor but no profile row

        await _settle(s, task)
        await task_ops.continue_task(s, bob, task.id, goal="add pricing")
        unknown = await _row(s, ada, project)
        assert (unknown.creator_name, unknown.turn_author_name) == ("Ada", None)

        task.turn_actor_id = None
        await s.flush()
        legacy = await _row(s, ada, project)
        assert (legacy.creator_name, legacy.turn_author_name) == ("Ada", "Ada")
        assert (legacy.creator_id, legacy.turn_author_id) == (ada.id, ada.id), "a NULL turn is the creator's"


# ── knocks follow access: the turn's author is told only while they can still
# see the task, and about a gate only while they can still answer it.


async def test_a_turn_author_removed_from_the_project_gets_no_knock(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        ada, bob, task, _step = await _bob_authors_turn_two(s)
        assert task.project_id is not None
        await project_ops.remove_member(s, ada, task.project_id, bob.id)

        await inbox.task_completed(s, task, "done")
        await inbox.budget_reached(s, task, "cap hit")

        assert await _rows(s, task.id, InboxKind.TASK_COMPLETED) == [ada.id]
        assert await _rows(s, task.id, InboxKind.BUDGET) == [ada.id]


async def test_a_turn_author_moved_out_with_the_task_gets_no_knock(silent_dispatcher: None) -> None:
    """Ada takes the task home after Bob's turn: a personal task is hers alone to see."""
    async with session_scope() as s:
        ada, bob, task, _step = await _bob_authors_turn_two(s)
        task.project_id = None
        await s.flush()

        await inbox.task_completed(s, task, "done")

        assert await _rows(s, task.id, InboxKind.TASK_COMPLETED) == [ada.id]
        assert task.turn_actor_id == bob.id, "the record keeps its author; only the knock follows access"


async def test_a_turn_author_demoted_to_viewer_hears_no_gate_but_still_the_delivery(
    silent_dispatcher: None,
) -> None:
    """A viewer can see the task (so the delivery reaches them) but cannot answer its gates."""
    async with session_scope() as s:
        ada, bob, task, step = await _bob_authors_turn_two(s)
        member_row = await s.get(ProjectMember, {"project_id": task.project_id, "actor_id": bob.id})
        assert member_row is not None
        member_row.role = "viewer"
        await s.flush()

        gate = await gates.open_approval(
            s,
            task,
            StepId(step.id),
            [("call_0", "Browser", "browser_open", "https://example.com")],
            source="browser",
            host="example.com",
            scopes=("once", "always"),
        )
        await s.flush()
        held = await s.scalars(select(InboxItem.actor_id).where(InboxItem.gate_id == gate.id))
        assert list(held) == [ada.id]

        await inbox.task_completed(s, task, "done")
        assert await _rows(s, task.id, InboxKind.TASK_COMPLETED) == sorted([ada.id, bob.id])


# ── a hand-off knocks everyone who can take control ──


async def test_a_handoff_knocks_every_editor_of_the_project_and_a_takeover_nobody(
    silent_dispatcher: None,
) -> None:
    """The agent asks for someone at the browser: anyone who can take control can answer it, so
    the Project's owner and every editor are knocked, the creator too (even when the creator is only
    an editor), once each. The viewer is not: they cannot take control. A
    takeover gate (someone already took control) knocks nobody."""
    async with session_scope() as s:
        ada, workspace = await member(s, "Ada")
        bob, _ = await member(s, "Bob", role=WorkspaceRole.MEMBER, workspace=workspace)
        dee, _ = await member(s, "Dee", role=WorkspaceRole.MEMBER, workspace=workspace)
        cy, _ = await member(s, "Cy", role=WorkspaceRole.MEMBER, workspace=workspace)
        project = (await project_ops.create(s, ada, name=f"launch-{uuid.uuid4().hex[:6]}")).project
        for who, role in ((bob, "editor"), (dee, "editor"), (cy, "viewer")):
            s.add(ProjectMember(project_id=project.id, actor_id=who.id, role=role, added_by=ada.id))
        await s.flush()
        task = await task_ops.create_task(s, bob, goal="update the admin app", project_id=project.id)
        step = PlanStep(task_id=task.id, ordinal=0, title="sign in")
        s.add(step)
        await s.flush()

        handoff = await gates.open_control(
            s, task, StepId(step.id), source="handoff", reason="sign in to admin.example.com", holder=""
        )
        await s.flush()
        knocked = list(await s.scalars(select(InboxItem.actor_id).where(InboxItem.gate_id == handoff.id)))
        assert sorted(knocked) == sorted([bob.id, ada.id, dee.id]), "once each, and never the viewer"

        takeover = await gates.open_control(
            s, task, StepId(step.id), source="takeover", reason="", holder="Dee", lease_since="x"
        )
        await s.flush()
        assert list(await s.scalars(select(InboxItem.actor_id).where(InboxItem.gate_id == takeover.id))) == []
