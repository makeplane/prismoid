# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Connectors × tasks (ADR-020): the door, the merge, the fork filter, and the step resolve.

The law under test: attaching is explicit and validated at the door (422 naming the fix);
follow-ups union; forks inherit only what still mounts; and at step time a connector that
cannot mount is dropped with a journaled `tool.called ok=false` - it costs the capability,
never the task.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models.actor import Actor
from prismoid.errors import ValidationFailed
from prismoid.operations import connectors as connector_ops
from prismoid.operations import tasks as task_ops
from prismoid.schemas.connectors import (
    ConnectorStatus,
    ConnectorToolInfo,
    ProbeResult,
    ServiceReach,
    ToolPolicy,
)
from tests.support.connector_rows import patch_email_probe, second_actor, shared_project

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("silent_dispatcher")]

MCP = "https://mcp.example.test/mcp"


@pytest.fixture
def live_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    async def probe(url: str, **kw: Any) -> ProbeResult:
        return ProbeResult(tools=(ConnectorToolInfo(name="search"),), instructions="", server_info={})

    monkeypatch.setattr(connector_ops, "probe_server", probe)


async def test_create_task_attaches_by_slug_and_id(db: AsyncSession, actor: Actor, live_probe: None):
    a = await connector_ops.create_connector(db, actor, server_url=MCP, name="Gmail")
    b = await connector_ops.create_connector(db, actor, server_url=MCP + "2", name="Notion")
    task = await task_ops.create_task(
        db,
        actor,
        goal="use both",
        connectors=["gmail", str(b.id), "gmail"],  # dup collapses
    )
    assert task.connector_ids == [str(a.id), str(b.id)]


async def test_create_task_refuses_unknown_and_unauthorized(
    db: AsyncSession, actor: Actor, monkeypatch: pytest.MonkeyPatch
):
    from prismoid.ai.tools.connectors import ProbeAuthRequired

    async def needs_auth(url: str, **kw: Any) -> ProbeResult:
        raise ProbeAuthRequired("")

    monkeypatch.setattr(connector_ops, "probe_server", needs_auth)
    parked = await connector_ops.create_connector(db, actor, server_url=MCP, name="Parked")
    assert parked.status == ConnectorStatus.PENDING_AUTH

    with pytest.raises(ValidationFailed) as refused:
        await task_ops.create_task(db, actor, goal="x", connectors=["ghost", "parked"])
    assert refused.value.fields["ghost"] == "no connector by this id or slug"
    assert "authorize" in refused.value.fields["parked"]


async def test_follow_up_merges_connectors(db: AsyncSession, actor: Actor, live_probe: None):
    a = await connector_ops.create_connector(db, actor, server_url=MCP, name="Gmail")
    b = await connector_ops.create_connector(db, actor, server_url=MCP + "2", name="Notion")
    task = await task_ops.create_task(db, actor, goal="start", connectors=["gmail"])
    task.status = "delivered"  # a settled thread is what a follow-up continues
    await db.commit()

    task = await task_ops.continue_task(db, actor, task.id, goal="now with notion", connectors=["notion"])
    assert task.connector_ids == [str(a.id), str(b.id)], "union by id, order preserved"


@pytest.fixture
def email_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_email_probe(monkeypatch)


async def test_intent_attach_mounts_the_matching_connector_with_provenance(
    db: AsyncSession, actor: Actor, email_probe: None
):
    """The user's own case: "email this to me" + a LIVE auto_attach connector
    exposing `send_email` - attached by the door, worn on the record as slug:keyword."""
    from prismoid.db.models.event import TaskEvent
    from prismoid.schemas.events import TaskCreated

    row = await connector_ops.create_connector(db, actor, server_url=MCP, name="Mailbox")
    assert row.status == ConnectorStatus.LIVE and row.auto_attach is True

    task = await task_ops.create_task(db, actor, goal="email me a summary of the quarter")
    assert task.connector_ids == [str(row.id)]
    created = [e for e in await TaskEvent.replay(db, task.id) if isinstance(e, TaskCreated)]
    assert created and created[0].connectors_auto == ["mailbox:email"]


async def test_intent_attach_respects_the_opt_out_and_explicit_attaches_win(
    db: AsyncSession, actor: Actor, email_probe: None
):
    row = await connector_ops.create_connector(db, actor, server_url=MCP, name="Mailbox")
    await connector_ops.update_connector(db, actor, row.id, auto_attach=False)
    task = await task_ops.create_task(db, actor, goal="email me a summary")
    assert task.connector_ids == [], "the owner said no - the door listens"

    await connector_ops.update_connector(db, actor, row.id, auto_attach=True)
    other = await connector_ops.create_connector(db, actor, server_url=MCP + "2", name="Notion")
    task2 = await task_ops.create_task(db, actor, goal="email me a summary", connectors=["notion"])
    assert task2.connector_ids == [str(other.id)], "an explicit attach wins outright"


async def test_intent_attach_never_fires_on_a_bare_generic_verb(
    db: AsyncSession, actor: Actor, email_probe: None
):
    """A goal like "Create a Notion page..." auto-attached Gmail via
    `create_draft`'s first token. Bare CRUD verbs are the language, not intent - only
    phrases, nouns, and brand words attach a credential."""
    await connector_ops.create_connector(db, actor, server_url=MCP, name="Mailbox")
    task = await task_ops.create_task(db, actor, goal="Create a summary page about the quarter")
    assert task.connector_ids == [], "a bare verb token must never mount a credential"

    # The phrase still works: two tokens together are specific.
    task2 = await task_ops.create_task(db, actor, goal="send email to the team when done")
    assert len(task2.connector_ids) == 1, "the send_email PHRASE is real intent"


async def test_intent_attach_never_fires_without_a_match_or_for_a_routine(
    db: AsyncSession, actor: Actor, email_probe: None
):
    from prismoid.db.models.event import TaskEvent
    from prismoid.schemas.events import TaskCreated

    row = await connector_ops.create_connector(db, actor, server_url=MCP, name="Mailbox")
    task = await task_ops.create_task(db, actor, goal="summarize the quarter")
    assert task.connector_ids == [], "no word-boundary hit, no mount"

    import uuid as _uuid

    from prismoid.db.models.routine import Routine

    routine = Routine(
        id=_uuid.uuid4(),
        name="weekly",
        instruction="email me a summary",
        status="active",
        owner_actor_id=actor.id,
        trigger_kind="api",
        trigger={},
        template={},
        budget_usd_per_run=1,
        overlap="skip",
        catch_up="skip",
        notify="normal",
        posture="run",
        min_interval_s=300,
        daily_fire_cap=24,
        pause_after_failures=3,
        consecutive_failures=0,
        runs_minted=0,
    )
    db.add(routine)
    await db.flush()
    routine_task = await task_ops.create_task(
        db, actor, goal="email me a summary", origin_routine_id=routine.id
    )
    assert routine_task.connector_ids == [], "a routine-minted task runs unattended - never auto"
    created = [e for e in await TaskEvent.replay(db, routine_task.id) if isinstance(e, TaskCreated)]
    assert created and created[0].connectors_auto == []
    assert row.id is not None  # the live row existed; the origin, not the roster, said no


async def test_fork_inherits_only_what_still_mounts(db: AsyncSession, actor: Actor, live_probe: None):
    a = await connector_ops.create_connector(db, actor, server_url=MCP, name="Gmail")
    b = await connector_ops.create_connector(db, actor, server_url=MCP + "2", name="Notion")
    parent = await task_ops.create_task(db, actor, goal="parent", connectors=["gmail", "notion"])
    parent.status = "delivered"
    b.status = ConnectorStatus.NEEDS_REAUTH  # died since the parent's submit
    await db.commit()

    child = await task_ops.fork(db, actor, parent.id)
    assert child.connector_ids == [str(a.id)], "a dead connector must not orphan a branch"
    assert child.held_at is not None


async def test_resolve_for_task_mounts_live_and_drops_dead_with_journal(
    db: AsyncSession, actor: Actor, live_probe: None
):
    a = await connector_ops.create_connector(db, actor, server_url=MCP, name="Gmail")
    b = await connector_ops.create_connector(db, actor, server_url=MCP + "2", name="Notion")
    task = await task_ops.create_task(db, actor, goal="both", connectors=["gmail", "notion"])
    b.status = ConnectorStatus.NEEDS_REAUTH
    await db.commit()

    journaled: list[tuple[str, bool, str]] = []

    async def journal(
        tool: str,
        ok: bool,
        summary: str = "",
        duration_ms: int = 0,
        denied_reason: str = "",
        tool_call_id: str = "",
        ref: str | None = None,
    ) -> None:
        journaled.append((tool, ok, denied_reason))

    resolved = await connector_ops.resolve_for_task(db, task, journal=journal)
    assert [s.slug for s in resolved.specs] == ["gmail"]
    assert resolved.specs[0].tool_names == ("search",)
    assert any(n.startswith("Mounted connector gmail") for n in resolved.notes)
    assert any(n.startswith("Dropped connector notion") for n in resolved.notes)
    assert journaled == [
        ("connector.notion", False, "needs authorization - run: prismoid connectors authorize notion")
    ]
    assert str(a.id) in task.connector_ids, "the task keeps its ids - a drop is per-mount, not a rewrite"


async def test_the_leads_reads_say_what_a_step_can_do_beyond_them(
    db: AsyncSession, actor: Actor, monkeypatch: pytest.MonkeyPatch
):
    """The Lead mounts only the unasked reads, yet hears the service's writes by name and what its owner
    turned off, so it never tells the person a service is read-only."""
    patch_email_probe(monkeypatch)
    gmail = await connector_ops.create_connector(db, actor, server_url=MCP, name="Gmail")
    task = await task_ops.create_task(db, actor, goal="mail Sam", connectors=["gmail"])
    await db.commit()

    reads = await connector_ops.resolve_for_task(db, task, reads_only=True)
    assert reads.specs[0].tool_names == ("search",)
    assert reads.reach == (ServiceReach(name="Gmail", steps_can=("send_email",)),)

    await connector_ops.set_tool_policies(db, actor, gmail.id, policies={"send_email": ToolPolicy.OFF.value})
    reads = await connector_ops.resolve_for_task(db, task, reads_only=True)
    assert reads.reach == (ServiceReach(name="Gmail", turned_off=("send_email",)),)


async def test_fork_by_a_co_member_sheds_the_creators_connectors(
    db: AsyncSession, actor: Actor, live_probe: None
):
    """The ownership boundary (senior review): a project editor's fork must NOT
    inherit the parent creator's connectors - a connector mounts its OWNER's credential, and
    cross-user delegation is a recorded deferral, never a fork side-effect."""
    other = await second_actor(db)
    project = await shared_project(db, owner=actor, member=other)
    await connector_ops.create_connector(db, actor, server_url=MCP, name="Gmail")
    parent = await task_ops.create_task(db, actor, goal="parent", project_id=project.id, connectors=["gmail"])
    parent.status = "delivered"
    await db.commit()

    child = await task_ops.fork(db, other, parent.id)
    assert child.connector_ids == [], "another user's credential never rides a fork"

    own = await task_ops.fork(db, actor, parent.id)
    assert own.connector_ids == parent.connector_ids, "the creator's own branch keeps them"


async def test_resolve_for_task_refuses_a_foreign_connector_id(
    db: AsyncSession, actor: Actor, live_probe: None
):
    """Defense in depth at the mount: even a foreign id already ON a task row loads nothing -
    the step-time fetch is creator-scoped, so the credential cannot mount, only journal."""
    other = await second_actor(db)
    theirs = await connector_ops.create_connector(db, other, server_url=MCP, name="Their gmail")
    task = await task_ops.create_task(db, actor, goal="mine")
    task.connector_ids = [str(theirs.id)]  # simulate any future door slip
    await db.commit()

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
        journaled.append(denied_reason)

    resolved = await connector_ops.resolve_for_task(db, task, journal=journal)
    assert resolved.specs == ()
    assert journaled and "no longer exists" in journaled[0], (
        "a foreign row is indistinguishable from a dead one - existence is not leaked"
    )


async def test_follow_up_connectors_are_creator_only(db: AsyncSession, actor: Actor, live_probe: None):
    """A co-editor may continue the thread, but only the creator can attach connectors: the
    mount is creator-scoped, so anyone else's refs would be a silent step-time drop - the
    door says it now, as a 422 naming the law."""
    other = await second_actor(db)
    project = await shared_project(db, owner=actor, member=other)
    await connector_ops.create_connector(db, other, server_url=MCP, name="Theirs")
    task = await task_ops.create_task(db, actor, goal="start", project_id=project.id)
    task.status = "delivered"
    await db.commit()

    with pytest.raises(ValidationFailed, match="only the task's creator"):
        await task_ops.continue_task(db, other, task.id, goal="use mine", connectors=["theirs"])

    followed = await task_ops.continue_task(db, other, task.id, goal="a plain follow-up")
    assert followed.id == task.id, "the guard is connector-specific - the thread stays shared"
