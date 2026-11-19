# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Connectors × a teammate's word (ADR-020): a connector mounts its owner's credential, so a turn
that carries anyone else's words borrows it.

The law under test: Bob's turn, steer or answer in Ada's task puts EVERY tool on her connectors
on ask - the read the dial would allow included - on both mount doors, and the mount says why on
the record; approval comes to Ada alone; and the borrow is not per turn: once a teammate's word is
anywhere in the thread (a fork's included, up to the fork point), every later turn of it, hers too,
asks. A task no teammate touched keeps the dial, as do a worker's edit and the creator's own input.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic_ai.exceptions import ApprovalRequired
from pydantic_ai.mcp import MCPToolset
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.capabilities.taint import tainted
from prismoid.ai.rules.approvals import Approvals
from prismoid.ai.tools.connectors import connector_toolsets
from prismoid.db.base import utcnow
from prismoid.db.models.actor import Actor
from prismoid.operations import connectors as connector_ops
from prismoid.operations import tasks as task_ops
from prismoid.schemas.connectors import ConnectorToolInfo, ProbeResult, ToolPolicy
from tests.support.asks import question_set
from tests.support.connector_rows import (
    ALL_ASK,
    adas_turn_with_bob_in_the_project,
    connect_gmail,
    gmail_mount,
    patch_email_probe,
    second_actor,
    shared_project,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("silent_dispatcher")]

MCP = "https://mcp.example.test/mcp"


@pytest.fixture
def email_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_email_probe(monkeypatch)


# ── a teammate's turn on the creator's connection ─────────────────────────────────────────────


async def _journal_sink() -> tuple[list[str], Any]:
    journaled: list[str] = []

    async def journal(
        tool: str,
        ok: bool,
        summary: str = "",
        duration_ms: int = 0,
        denied_reason: str = "",
        tool_call_id: str = "",
        ref: str | None = None,
    ) -> None:
        journaled.append(tool)

    return journaled, journal


async def _bobs_turn_on_adas_gmail(db: AsyncSession, ada: Actor) -> tuple[Actor, Any]:
    """Ada's Project task with her Gmail explicitly attached; Bob (an editor) takes turn 2."""
    bob = await second_actor(db)
    project = await shared_project(db, owner=ada, member=bob)
    await connect_gmail(db, ada)
    task = await task_ops.create_task(
        db, ada, goal="triage my inbox", project_id=project.id, connectors=["gmail"]
    )
    task.status = "delivered"
    await db.commit()
    task = await task_ops.continue_task(db, bob, task.id, goal="summarize Ada's inbox")
    assert task.turn_actor_id == bob.id
    return bob, task


async def test_a_teammates_turn_mounts_the_creators_connectors_all_ask(
    db: AsyncSession, actor: Actor, email_probe: None
):
    """Bob's turn mounts every tool on Ada's connector on ask, her allowed read included, with a note."""
    _bob, task = await _bobs_turn_on_adas_gmail(db, actor)
    journaled, journal = await _journal_sink()

    resolved = await connector_ops.resolve_for_task(db, task, journal=journal)

    (spec,) = resolved.specs
    assert spec.tool_policies == {"send_email": ToolPolicy.ASK, "search": ToolPolicy.ASK}
    assert set(spec.tool_names) == {"send_email", "search"}, "asking is not dropping"
    assert any("asks the task's creator" in n for n in resolved.notes), resolved.notes
    assert journaled == [], "a mount is a note, not a tool call"


async def test_the_creators_own_turn_mounts_per_the_dial(db: AsyncSession, actor: Actor, email_probe: None):
    await connect_gmail(db, actor)
    task = await task_ops.create_task(db, actor, goal="triage my inbox", connectors=["gmail"])
    await db.commit()
    _journaled, journal = await _journal_sink()

    resolved = await connector_ops.resolve_for_task(db, task, journal=journal)

    (spec,) = resolved.specs
    assert spec.tool_policies == {"send_email": ToolPolicy.ALLOW, "search": ToolPolicy.ALLOW}
    assert not any("asks the task's creator" in n for n in resolved.notes)


async def test_an_intent_attached_connector_runs_its_read_and_parks_a_write_for_approval(
    db: AsyncSession, actor: Actor, monkeypatch: pytest.MonkeyPatch
):
    """Nobody clicked Connect for this task, so a tool not labelled read-only parks before the
    server; the labelled read runs, and so does an owner's `allow`."""

    async def probe(url: str, **kw: Any) -> ProbeResult:
        return ProbeResult(
            tools=(
                ConnectorToolInfo(name="search", read_only=True),
                ConnectorToolInfo(name="send_email"),
                ConnectorToolInfo(name="archive"),
            ),
            instructions="",
            server_info={},
        )

    monkeypatch.setattr(connector_ops, "probe_server", probe)
    row = await connector_ops.create_connector(db, actor, server_url=MCP, name="Mailbox")
    await connector_ops.set_tool_policies(db, actor, row.id, policies={"archive": "allow"})
    task = await task_ops.create_task(db, actor, goal="email me a summary of the quarter")
    await db.commit()
    assert task.connector_ids == [str(row.id)], "attached by the goal's words"

    (spec,) = (await connector_ops.resolve_for_task(db, task)).specs
    assert spec.tool_policies == {
        "search": ToolPolicy.ALLOW,
        "send_email": ToolPolicy.ASK,
        "archive": ToolPolicy.ALLOW,
    }

    toolset: Any = connector_toolsets((spec,), None, approve=Approvals(tainted=tainted))[0]
    while not isinstance(toolset, MCPToolset):
        toolset = toolset.wrapped
    hook = toolset.process_tool_call
    assert hook is not None
    ran: list[str] = []

    async def call_tool(name: str, args: dict[str, Any], *, metadata: dict[str, Any] | None = None) -> str:
        ran.append(name)
        return "ok"

    class Ctx:
        tool_call_approved = False
        messages = ()  # a fresh run: nothing declined yet

    ctx: Any = Ctx()
    with pytest.raises(ApprovalRequired) as asked:
        await hook(ctx, call_tool, "send_email", {"to": "a@b.c"})
    assert ran == [] and asked.value.metadata is not None
    assert asked.value.metadata["tool"] == "mailbox_send_email"
    await hook(ctx, call_tool, "search", {"q": "invoices"})
    await hook(ctx, call_tool, "archive", {"id": "1"})
    assert ran == ["search", "archive"]


async def test_a_bots_declared_connector_asks_on_a_teammates_turn(
    db: AsyncSession, actor: Actor, email_probe: None
):
    """The bot-manifest door mounts the same creator's rows, so it takes the same rule."""
    _bob, task = await _bobs_turn_on_adas_gmail(db, actor)
    resolved = await connector_ops.resolve_for_bot(db, task, ["gmail"])

    (spec,) = resolved.specs
    assert set(spec.tool_policies.values()) == {ToolPolicy.ASK}


async def test_a_read_only_call_on_a_teammates_turn_parks_for_the_creator_alone(
    db: AsyncSession, actor: Actor, email_probe: None
):
    """On Bob's turn a read parks on an approve gate that only Ada, the credential's owner, can accept."""
    from prismoid.db.models.plan import PlanStep
    from prismoid.errors import Forbidden
    from prismoid.operations import gates
    from prismoid.schemas.ids import StepId
    from prismoid.schemas.plan import GateAction

    bob, task = await _bobs_turn_on_adas_gmail(db, actor)
    resolved = await connector_ops.resolve_for_task(db, task)
    (spec,) = resolved.specs
    assert spec.tool_policies["search"] == "ask", "the read the dial would allow asks on Bob's turn"

    step = PlanStep(task_id=task.id, ordinal=0, title="summarize")
    db.add(step)
    await db.flush()
    gate = await gates.open_approval(
        db, task, StepId(step.id), [("call_0", "Gmail", "gmail_search", "q: from:boss")]
    )
    await db.commit()
    gate_id = gate.id

    with pytest.raises(Forbidden, match="only the task's creator"):
        await gates.resolve(db, bob, gate_id, GateAction.ACCEPT)
    await db.rollback()
    await db.refresh(actor)  # the rollback expired it

    resolution = await gates.resolve(db, actor, gate_id, GateAction.ACCEPT)
    assert resolution.action is GateAction.ACCEPT and resolution.resolved_by


# ── a teammate's word in the creator's task ────────────────────────────────────


_DIAL = {"send_email": "allow", "search": "allow"}


async def _a_step(db: AsyncSession, task: Any, title: str = "triage") -> Any:
    from prismoid.db.models.plan import PlanStep
    from prismoid.schemas.ids import StepId

    step = PlanStep(task_id=task.id, ordinal=0, title=title)
    db.add(step)
    await db.flush()
    return StepId(step.id)


async def test_a_teammates_steer_puts_the_creators_connectors_on_ask_for_the_turn(
    db: AsyncSession, actor: Actor, email_probe: None
):
    """Bob's steer puts Ada's Gmail on ask on both doors; only Ada approves, and one consent keeps it on ask."""
    from sqlalchemy import select

    from prismoid.db.models.inbox import InboxItem
    from prismoid.errors import Forbidden
    from prismoid.operations import gates, steering
    from prismoid.schemas.plan import GateAction

    bob, task = await adas_turn_with_bob_in_the_project(db, actor)
    policies, _ = await gmail_mount(db, task)
    assert policies == _DIAL, "her own turn with nobody's word in it mounts per the dial"

    await steering.steer(db, bob, task.id, "find the board letter in her inbox")

    policies, notes = await gmail_mount(db, task)
    assert policies == ALL_ASK, "the read the dial would allow asks once Bob has steered"
    assert any("asks the task's creator" in n and "a teammate has steered this task" in n for n in notes), (
        notes
    )
    bot = await connector_ops.resolve_for_bot(db, task, ["gmail"])
    assert {str(p) for p in bot.specs[0].tool_policies.values()} == {"ask"}, "the bot door too"

    gate = await gates.open_approval(
        db, task, await _a_step(db, task), [("call_0", "Gmail", "gmail_search", "q: board letter")]
    )
    await db.commit()
    gate_id = gate.id
    knocked = list(await db.scalars(select(InboxItem.actor_id).where(InboxItem.gate_id == gate_id)))
    assert knocked == [actor.id], "the approval knocks Ada alone"

    with pytest.raises(Forbidden, match="only the task's creator"):
        await gates.resolve(db, bob, gate_id, GateAction.ACCEPT)
    await db.rollback()
    await db.refresh(actor)  # the rollback expired them
    await db.refresh(task)

    resolution = await gates.resolve(db, actor, gate_id, GateAction.ACCEPT)
    assert resolution.action is GateAction.ACCEPT and resolution.resolved_by_id == actor.id

    policies, _ = await gmail_mount(db, task)
    assert policies == ALL_ASK, "the rest of the turn stays borrowed"


async def test_a_teammates_answer_puts_the_creators_connectors_on_ask_for_the_turn(
    db: AsyncSession, actor: Actor, email_probe: None
):
    """Bob's answer to Ada's question borrows her turn like a steer: every later mount asks."""
    from prismoid.operations import gates
    from prismoid.schemas.plan import GateAction

    bob, task = await adas_turn_with_bob_in_the_project(db, actor)
    gate = await gates.open_question(
        db, task, await _a_step(db, task), question_set(("Which folder?", ["inbox", "archive"]))
    )
    await db.commit()
    await gates.resolve(db, bob, gate.id, GateAction.ACCEPT, {"q1": "archive"})

    policies, notes = await gmail_mount(db, task)
    assert policies == ALL_ASK
    assert any("a teammate has answered in this task" in n for n in notes), notes


async def _adas_next_turn(db: AsyncSession, ada: Actor, task: Any, goal: str = "now the sent folder") -> Any:
    task.status = "delivered"
    await db.commit()
    task = await task_ops.continue_task(db, ada, task.id, goal=goal)
    assert task.turn_actor_id == ada.id
    return task


async def test_the_creators_next_turn_stays_borrowed_once_a_teammate_has_steered(
    db: AsyncSession, actor: Actor, email_probe: None
):
    """After Bob steered and answered in turn 1, Ada's own turn 2 still asks on both mount doors."""
    from prismoid.operations import gates, steering
    from prismoid.schemas.plan import GateAction

    bob, task = await adas_turn_with_bob_in_the_project(db, actor)
    await steering.steer(db, bob, task.id, "only the last week")
    gate = await gates.open_question(
        db, task, await _a_step(db, task), question_set(("Which folder?", ["inbox"]))
    )
    await db.commit()
    await gates.resolve(db, bob, gate.id, GateAction.ACCEPT, {"q1": "inbox"})
    assert (await gmail_mount(db, task))[0] == ALL_ASK

    task = await _adas_next_turn(db, actor, task)
    assert task.turn == 2

    policies, notes = await gmail_mount(db, task)
    assert policies == ALL_ASK, "Ada's own next turn still reads Bob's words"
    assert any("asks the task's creator" in n and "a teammate has steered this task" in n for n in notes), (
        notes
    )
    bot = await connector_ops.resolve_for_bot(db, task, ["gmail"])
    assert {str(p) for p in bot.specs[0].tool_policies.values()} == {"ask"}, "the bot door too"


async def test_the_creators_turn_after_a_teammates_turn_is_borrowed(
    db: AsyncSession, actor: Actor, email_probe: None
):
    """Ada's turn after Bob's turn is borrowed, because his turn is in her thread."""
    _bob, task = await _bobs_turn_on_adas_gmail(db, actor)

    task = await _adas_next_turn(db, actor, task)
    assert task.turn == 3

    policies, notes = await gmail_mount(db, task)
    assert policies == ALL_ASK
    assert any("a teammate has taken a turn on this task" in n for n in notes), notes


async def test_the_creators_turn_after_a_teammates_answer_is_borrowed(
    db: AsyncSession, actor: Actor, email_probe: None
):
    """Bob answered the question Ada's turn 1 asked and did nothing else; her turn 2 asks."""
    from prismoid.operations import gates
    from prismoid.schemas.plan import GateAction

    bob, task = await adas_turn_with_bob_in_the_project(db, actor)
    gate = await gates.open_question(
        db, task, await _a_step(db, task), question_set(("Which folder?", ["inbox", "archive"]))
    )
    await db.commit()
    await gates.resolve(db, bob, gate.id, GateAction.ACCEPT, {"q1": "archive"})

    task = await _adas_next_turn(db, actor, task)

    policies, notes = await gmail_mount(db, task)
    assert policies == ALL_ASK
    assert any("a teammate has answered in this task" in n for n in notes), notes


async def test_a_task_no_teammate_touched_keeps_the_dial_on_every_turn(
    db: AsyncSession, actor: Actor, email_probe: None
):
    """Bob is in the Project but never put a word into Ada's task: every turn of it is hers."""
    _bob, task = await adas_turn_with_bob_in_the_project(db, actor)
    assert (await gmail_mount(db, task))[0] == _DIAL

    task = await _adas_next_turn(db, actor, task)
    task = await _adas_next_turn(db, actor, task, goal="and the drafts")
    assert task.turn == 3

    policies, notes = await gmail_mount(db, task)
    assert policies == _DIAL
    assert not any("asks the task's creator" in n for n in notes), notes


async def test_a_system_edit_or_the_creators_own_input_does_not_borrow_the_turn(
    db: AsyncSession, actor: Actor, email_probe: None
):
    """A worker's edit, Ada's own steer or answer and a timed-out question never borrow the turn."""
    from prismoid.db.models.event import TaskEvent
    from prismoid.db.models.plan import PlanEdit
    from prismoid.operations import gates, steering
    from prismoid.schemas.events import PlanEdited
    from prismoid.schemas.ids import ProjectId, TaskId
    from prismoid.schemas.plan import GateAction, PlanEditKind
    from prismoid.schemas.plan import PlanEdit as PlanEditView

    _bob, task = await adas_turn_with_bob_in_the_project(db, actor)

    # A worker-made edit, the shape a re-plan with no editor writes: no actor on either row.
    now = utcnow()
    db.add(PlanEdit(task_id=task.id, kind="replan", reason="a step failed", payload={}, actor_id=None))
    await TaskEvent.append(
        db,
        PlanEdited(
            seq=await task.next_seq(db),
            task_id=TaskId(task.id),
            project_id=ProjectId(task.project_id) if task.project_id else None,
            at=now,
            actor=None,
            edit=PlanEditView(kind=PlanEditKind.REPLAN, reason="a step failed", at=now),
        ),
    )
    await db.commit()

    await steering.steer(db, actor, task.id, "skip the newsletters")
    step = await _a_step(db, task)
    mine = await gates.open_question(db, task, step, question_set(("Which folder?", ["inbox"])))
    await db.commit()
    await gates.resolve(db, actor, mine.id, GateAction.ACCEPT, {"q1": "inbox"})
    await gates.open_question(db, task, step, question_set(("How far back?", [])))
    await gates.expire_open(db, task)
    await db.commit()

    policies, notes = await gmail_mount(db, task)
    assert policies == _DIAL
    assert not any("asks the task's creator" in n for n in notes), notes

    task = await _adas_next_turn(db, actor, task)  # nor her next turn: nobody else spoke
    policies, notes = await gmail_mount(db, task)
    assert policies == _DIAL
    assert not any("asks the task's creator" in n for n in notes), notes


# ── forks carry their parent's voices up to the fork point ─────────────────────────────────────


async def test_a_fork_of_a_task_a_teammate_steered_borrows_from_its_first_turn(
    db: AsyncSession, actor: Actor, email_probe: None
):
    """A fork of a task Bob steered asks on both mount doors from its first turn."""
    from prismoid.operations import steering

    bob, parent = await adas_turn_with_bob_in_the_project(db, actor)
    await steering.steer(db, bob, parent.id, "only the last week")
    fork = await task_ops.fork(db, actor, parent.id)
    await db.commit()
    assert fork.creator_actor_id == actor.id and fork.turn_actor_id == actor.id

    policies, notes = await gmail_mount(db, fork)
    assert policies == ALL_ASK
    assert any("a teammate worked on the task this was forked from" in n for n in notes), notes
    bot = await connector_ops.resolve_for_bot(db, fork, ["gmail"])
    assert {str(p) for p in bot.specs[0].tool_policies.values()} == {"ask"}, "the bot door too"


async def test_a_teammates_word_after_the_fork_point_does_not_reach_the_fork(
    db: AsyncSession, actor: Actor, email_probe: None
):
    """Bob's steer after the fork point borrows the parent while the fork keeps the dial."""
    from prismoid.operations import steering

    bob, parent = await adas_turn_with_bob_in_the_project(db, actor)
    fork = await task_ops.fork(db, actor, parent.id)
    await db.commit()
    await steering.steer(db, bob, parent.id, "only the last week")

    assert (await gmail_mount(db, parent))[0] == ALL_ASK, "the parent borrows"
    policies, notes = await gmail_mount(db, fork)
    assert policies == _DIAL
    assert not any("asks the task's creator" in n for n in notes), notes


async def test_a_fork_of_a_fork_follows_the_chain(db: AsyncSession, actor: Actor, email_probe: None):
    """A fork of a fork still borrows from a teammate's steer two hops up."""
    from prismoid.operations import steering

    bob, parent = await adas_turn_with_bob_in_the_project(db, actor)
    await steering.steer(db, bob, parent.id, "only the last week")
    child = await task_ops.fork(db, actor, parent.id)
    await db.commit()
    grandchild = await task_ops.fork(db, actor, child.id)
    await db.commit()

    assert (await gmail_mount(db, grandchild))[0] == ALL_ASK


async def test_a_fork_of_a_teammates_task_borrows(db: AsyncSession, actor: Actor, email_probe: None):
    """Ada's fork of Bob's task borrows: her Gmail, mounted by a bot manifest, asks for every call."""
    bea = await second_actor(db)
    project = await shared_project(db, owner=bea, member=actor)
    await connect_gmail(db, actor)
    theirs = await task_ops.create_task(db, bea, goal="triage the shared inbox", project_id=project.id)
    await db.commit()
    fork = await task_ops.fork(db, actor, theirs.id)
    await db.commit()

    bot = await connector_ops.resolve_for_bot(db, fork, ["gmail"])
    assert {str(p) for p in bot.specs[0].tool_policies.values()} == {"ask"}


async def test_a_fork_of_a_task_no_teammate_touched_keeps_the_dial(
    db: AsyncSession, actor: Actor, email_probe: None
):
    _bob, parent = await adas_turn_with_bob_in_the_project(db, actor)
    fork = await task_ops.fork(db, actor, parent.id)
    await db.commit()

    assert (await gmail_mount(db, fork))[0] == _DIAL
