# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Every actor-scoped read is fenced to the bound workspace (ADR-061).

The shape of every test: the SAME person belongs to two workspaces, owns a row in one, and sees it only
while the unit of work is bound to that workspace - a fetcher 404s it, a list leaves it out. A person
switching workspaces never carries their rows across. Skills are the one registry that mixes platform
and workspace rows: a workspace sees its own versions and the builtins, never another workspace's, and cannot
take a builtin's name.

Needs the stack: `just up && just migrate`.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models import Workspace, WorkspaceMember, WorkspaceRole, tenant
from prismoid.db.models.actor import Actor
from prismoid.db.models.api_key import ApiKey
from prismoid.db.models.artifact import Artifact
from prismoid.db.models.bot import BotRecord, BotStatus
from prismoid.db.models.bot_channel import BotChannel
from prismoid.db.models.bot_template import BotTemplate
from prismoid.db.models.bot_template_install import BotTemplateInstall
from prismoid.db.models.capture import Capture
from prismoid.db.models.connector import Connector
from prismoid.db.models.conversation import Conversation
from prismoid.db.models.egress_allowance import EgressAllowance
from prismoid.db.models.gate import Gate
from prismoid.db.models.inbox import InboxItem, InboxKind
from prismoid.db.models.memory import Memory, MemoryScope
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.plugin_install import PluginInstall
from prismoid.db.models.project import Project, Role
from prismoid.db.models.routine import Routine
from prismoid.db.models.skill import SkillOrigin, SkillRecord, SkillStatus
from prismoid.db.models.task import Task
from prismoid.db.models.user import User
from prismoid.db.session import session_scope
from prismoid.errors import Conflict, NotFound, ValidationFailed
from prismoid.operations import bot_templates as template_ops
from prismoid.operations import channels as channel_ops
from prismoid.operations import memory as memory_ops
from prismoid.operations import projects as project_ops
from prismoid.operations import routines as routine_ops
from prismoid.operations import skills as skill_ops
from prismoid.operations import steering
from prismoid.schemas.plan import GateKind, GateSpec, GateState
from tests.support.cast import a_workspace, member

pytestmark = pytest.mark.integration


async def _also_in(s: AsyncSession, who: Actor) -> Workspace:
    """A second workspace this same person belongs to - the session stays bound where it was."""
    _, beta = await a_workspace(s, "Beta")
    await WorkspaceMember.add(s, beta.id, who.id, WorkspaceRole.MEMBER)
    return beta


def _act_in(s: AsyncSession, workspace: Workspace) -> None:
    tenant.bind(s, workspace.id, "owner")


# ── project.py ──────────────────────────────────────────────────────────────────────────────────


async def test_project_get_for_and_list_are_fenced_to_the_bound_workspace() -> None:
    async with session_scope() as s:
        ada, acme = await member(s, "Ada")
        project = Project(name="Acme room", owner_actor_id=ada.id)
        s.add(project)
        await s.commit()
        beta = await _also_in(s, ada)

        _act_in(s, beta)
        with pytest.raises(NotFound):
            await Project.get_for(s, project.id, ada)
        assert project.id not in {sp.id for sp in await Project.list_for(s, ada)}

        _act_in(s, acme)
        assert (await Project.get_for(s, project.id, ada)).id == project.id
        assert project.id in {sp.id for sp in await Project.list_for(s, ada)}


async def test_add_member_refuses_a_person_outside_the_projects_workspace_like_an_unknown_email() -> None:
    async with session_scope() as s:
        ada, _acme = await member(s, "Ada")
        project = Project(name="Acme room", owner_actor_id=ada.id)
        s.add(project)
        outsider, _beta = await a_workspace(s, "Bo")  # a real account - in another workspace only
        s.add(User(actor_id=outsider.id, email=f"bo{uuid.uuid4().hex[:10]}@example.com", name="Bo"))
        await s.commit()
        email = (await s.get(User, outsider.id)).email  # pyright: ignore[reportOptionalMemberAccess]

        with pytest.raises(ValidationFailed) as refused:
            await project_ops.add_member(s, ada, project.id, email=email, role=Role.EDITOR)
        unknown = f"nobody{uuid.uuid4().hex[:10]}@example.com"
        with pytest.raises(ValidationFailed) as never_existed:
            await project_ops.add_member(s, ada, project.id, email=unknown, role=Role.EDITOR)
        # No existence leak: the refusal reads exactly like an email nobody holds.
        assert refused.value.fields == never_existed.value.fields
        assert refused.value.message.replace(email, "X") == never_existed.value.message.replace(unknown, "X")


async def test_moving_a_task_into_another_workspaces_project_is_a_404() -> None:
    async with session_scope() as s:
        ada, acme = await member(s, "Ada")
        task = Task(goal="acme work", creator_actor_id=ada.id, criteria={})
        s.add(task)
        await s.commit()
        beta = await _also_in(s, ada)
        _act_in(s, beta)
        elsewhere = Project(name="Beta room", owner_actor_id=ada.id)  # Ada owns it, in beta
        s.add(elsewhere)
        await s.commit()

        _act_in(s, acme)
        with pytest.raises(NotFound):
            await steering.move(s, ada, task.id, elsewhere.id)


# ── task.py ───────────────────────────────────────────────────────────────────────────────────


async def test_task_get_for_list_counts_and_client_token_are_fenced_to_the_bound_workspace() -> None:
    async with session_scope() as s:
        ada, acme = await member(s, "Ada")
        task = Task(goal="acme work", creator_actor_id=ada.id, criteria={}, client_token="tok-fence-1")
        s.add(task)
        await s.commit()
        beta = await _also_in(s, ada)

        _act_in(s, beta)
        with pytest.raises(NotFound):
            await Task.get_for(s, task.id, ada)
        assert task.id not in {t.id for t in await Task.list_for(s, ada)}
        assert sum((await Task.counts_for(s, ada)).values()) == 0
        assert await Task.by_client_token(s, ada.id, "tok-fence-1") is None

        _act_in(s, acme)
        assert (await Task.get_for(s, task.id, ada)).id == task.id
        assert task.id in {t.id for t in await Task.list_for(s, ada)}
        assert sum((await Task.counts_for(s, ada)).values()) == 1
        assert (await Task.by_client_token(s, ada.id, "tok-fence-1")).id == task.id  # pyright: ignore[reportOptionalMemberAccess]


# ── artifact.py / plan.py (children fenced through their task) ────────────────────────────────


async def test_artifact_get_for_and_agent_feeds_are_fenced_through_the_task() -> None:
    async with session_scope() as s:
        ada, acme = await member(s, "Ada")
        task = Task(goal="acme work", creator_actor_id=ada.id, criteria={})
        s.add(task)
        await s.flush()
        step = PlanStep(task_id=task.id, spec={"agent": "fence-writer"})
        s.add(step)
        await s.flush()
        art = Artifact(
            task_id=task.id,
            step_id=step.id,
            lineage_id=uuid.uuid4(),
            type="document",
            title="Acme memo",
            sha256="0" * 64,
            storage_key="k",
        )
        s.add(art)
        await s.commit()
        beta = await _also_in(s, ada)

        _act_in(s, beta)
        with pytest.raises(NotFound):
            await Artifact.get_for(s, art.id, ada)
        assert await Artifact.by_agent(s, "fence-writer", creator_actor_id=ada.id) == []
        assert await PlanStep.runs_of_agent(s, "fence-writer", creator_actor_id=ada.id) == []
        assert (await PlanStep.spend_of_agent(s, "fence-writer", creator_actor_id=ada.id))[2] == 0

        _act_in(s, acme)
        assert (await Artifact.get_for(s, art.id, ada)).id == art.id
        assert [a.id for a, _ in await Artifact.by_agent(s, "fence-writer", creator_actor_id=ada.id)] == [
            art.id
        ]
        assert len(await PlanStep.runs_of_agent(s, "fence-writer", creator_actor_id=ada.id)) == 1
        assert (await PlanStep.spend_of_agent(s, "fence-writer", creator_actor_id=ada.id))[2] == 1


# ── gate.py ───────────────────────────────────────────────────────────────────────────────────


async def test_gate_open_for_is_fenced_through_the_task() -> None:
    async with session_scope() as s:
        ada, acme = await member(s, "Ada")
        task = Task(goal="acme work", creator_actor_id=ada.id, criteria={})
        s.add(task)
        await s.flush()
        gate = Gate(
            task_id=task.id,
            kind=GateKind.BUDGET.value,
            state=GateState.OPEN.value,
            spec=GateSpec(kind=GateKind.BUDGET, title="Budget", reason="spent").model_dump(mode="json"),
        )
        s.add(gate)
        await s.commit()
        beta = await _also_in(s, ada)

        _act_in(s, beta)
        assert gate.id not in {g.id for g in await Gate.open_for(s, ada)}
        _act_in(s, acme)
        assert gate.id in {g.id for g in await Gate.open_for(s, ada)}


# ── conversation.py ───────────────────────────────────────────────────────────────────────────


async def test_conversation_get_for_and_for_bot_are_fenced_to_the_bound_workspace() -> None:
    async with session_scope() as s:
        ada, acme = await member(s, "Ada")
        conv = Conversation(owner_actor_id=ada.id, bot_name="fence-bot")
        s.add(conv)
        await s.commit()
        beta = await _also_in(s, ada)

        _act_in(s, beta)
        with pytest.raises(NotFound):
            await Conversation.get_for(s, conv.id, ada)
        assert await Conversation.for_bot(s, ada.id, "fence-bot") == []

        _act_in(s, acme)
        assert (await Conversation.get_for(s, conv.id, ada)).id == conv.id
        assert [c.id for c in await Conversation.for_bot(s, ada.id, "fence-bot")] == [conv.id]


# ── routine.py ────────────────────────────────────────────────────────────────────────────────


def _routine(owner: Actor, name: str) -> Routine:
    return Routine(
        name=name,
        instruction="email me a summary",
        status="active",
        owner_actor_id=owner.id,
        trigger_kind="api",
        trigger={},
        template={"bot": "fence-keeper"},
        budget_usd_per_run=Decimal(1),
        overlap="skip",
        catch_up="skip",
        notify="normal",
        posture="run",
        min_interval_s=300,
        daily_fire_cap=24,
        pause_after_failures=3,
    )


async def test_routine_get_for_lists_and_person_fire_are_fenced_to_the_bound_workspace() -> None:
    async with session_scope() as s:
        ada, acme = await member(s, "Ada")
        routine = _routine(ada, "weekly")
        s.add(routine)
        await s.commit()
        beta = await _also_in(s, ada)

        _act_in(s, beta)
        with pytest.raises(NotFound):
            await Routine.get_for(s, routine.id, ada)
        assert await Routine.list_for(s, ada, project_id=None, limit=50, cursor=None) == []
        assert await Routine.for_keeper(s, ada, bot_name="fence-keeper", project_id=None) == []
        # A person's credential fires into the caller's active workspace - a routine of another workspace
        # is simply not there (the rt_ token door binds the routine's own workspace instead).
        with pytest.raises(NotFound):
            await routine_ops.fire(s, ada, routine.id)

        _act_in(s, acme)
        assert (await Routine.get_for(s, routine.id, ada)).id == routine.id
        listed = await Routine.list_for(s, ada, project_id=None, limit=50, cursor=None)
        assert [r.id for r in listed] == [routine.id]
        assert [r.id for r in await Routine.for_keeper(s, ada, bot_name="fence-keeper", project_id=None)] == [
            routine.id
        ]


async def test_a_personal_routine_name_is_per_workspace() -> None:
    async with session_scope() as s:
        ada, _acme = await member(s, "Ada")
        s.add(_routine(ada, "daily"))
        await s.commit()
        beta = await _also_in(s, ada)
        _act_in(s, beta)
        s.add(_routine(ada, "daily"))
        await s.commit()  # the same name in another workspace is another routine, not a collision


# ── connector.py ──────────────────────────────────────────────────────────────────────────────


async def test_connector_fetchers_are_fenced_to_the_bound_workspace() -> None:
    async with session_scope() as s:
        ada, acme = await member(s, "Ada")
        conn = Connector(
            owner_actor_id=ada.id, name="Linear", slug="linear", server_url="https://mcp.linear.app"
        )
        s.add(conn)
        await s.commit()
        beta = await _also_in(s, ada)

        _act_in(s, beta)
        with pytest.raises(NotFound):
            await Connector.get_for(s, conn.id, ada)
        assert await Connector.for_owner(s, ada.id) == []
        assert await Connector.resolve_refs(s, ada.id, ["linear", str(conn.id)]) == {}
        assert await Connector.for_ids(s, [conn.id], owner_id=ada.id) == []

        _act_in(s, acme)
        assert (await Connector.get_for(s, conn.id, ada)).id == conn.id
        assert [c.id for c in await Connector.for_owner(s, ada.id)] == [conn.id]
        assert set(await Connector.resolve_refs(s, ada.id, ["linear"])) == {"linear"}
        assert [c.id for c in await Connector.for_ids(s, [conn.id], owner_id=ada.id)] == [conn.id]


# ── capture.py ────────────────────────────────────────────────────────────────────────────────


async def test_capture_get_for_and_open_list_are_fenced_to_the_bound_workspace() -> None:
    async with session_scope() as s:
        ada, acme = await member(s, "Ada")
        cap = Capture(actor_id=ada.id, text="call the vendor")
        s.add(cap)
        await s.commit()
        beta = await _also_in(s, ada)

        _act_in(s, beta)
        assert await Capture.get_for(s, cap.id, ada) is None
        assert await Capture.open_for(s, ada) == []

        _act_in(s, acme)
        assert (await Capture.get_for(s, cap.id, ada)).id == cap.id  # pyright: ignore[reportOptionalMemberAccess]
        assert [c.id for c in await Capture.open_for(s, ada)] == [cap.id]


# ── egress_allowance.py ───────────────────────────────────────────────────────────────────────


async def test_egress_allowance_fetchers_are_fenced_to_the_bound_workspace() -> None:
    async with session_scope() as s:
        ada, acme = await member(s, "Ada")
        allow = EgressAllowance(owner_actor_id=ada.id, host="pypi.org", source="settings")
        s.add(allow)
        await s.commit()
        beta = await _also_in(s, ada)

        _act_in(s, beta)
        with pytest.raises(NotFound):
            await EgressAllowance.get_for(s, allow.id, ada)
        assert await EgressAllowance.live_for_owner(s, ada.id) == []
        assert await EgressAllowance.live_host(s, ada.id, "pypi.org") is None
        # …and the same host may be allowed here too - a live grant is per workspace.
        s.add(EgressAllowance(owner_actor_id=ada.id, host="pypi.org", source="settings"))
        await s.commit()

        _act_in(s, acme)
        assert (await EgressAllowance.get_for(s, allow.id, ada)).id == allow.id
        assert [a.id for a in await EgressAllowance.live_for_owner(s, ada.id)] == [allow.id]
        assert (await EgressAllowance.live_host(s, ada.id, "pypi.org")).id == allow.id  # pyright: ignore[reportOptionalMemberAccess]


# ── inbox.py ──────────────────────────────────────────────────────────────────────────────────


async def test_inbox_reads_and_mark_all_read_are_fenced_to_the_bound_workspace() -> None:
    async with session_scope() as s:
        ada, acme = await member(s, "Ada")
        item = InboxItem(
            actor_id=ada.id, kind=InboxKind.TASK_COMPLETED.value, title="Acme delivered", body="…"
        )
        s.add(item)
        await s.commit()
        beta = await _also_in(s, ada)

        _act_in(s, beta)
        with pytest.raises(NotFound):
            await InboxItem.get_for(s, item.id, ada)
        assert await InboxItem.list_for(s, ada) == []
        assert await InboxItem.unread_count(s, ada) == 0
        from prismoid.db.base import utcnow

        assert await InboxItem.mark_all_read(s, ada, utcnow()) == 0  # never another workspace's items

        _act_in(s, acme)
        assert (await InboxItem.get_for(s, item.id, ada)).id == item.id
        assert [i.id for i in await InboxItem.list_for(s, ada)] == [item.id]
        assert await InboxItem.unread_count(s, ada) == 1


# ── memory.py ─────────────────────────────────────────────────────────────────────────────────


async def test_memory_lanes_and_single_reads_are_fenced_to_the_bound_workspace() -> None:
    async with session_scope() as s:
        ada, acme = await member(s, "Ada")
        mem = Memory(
            scope=MemoryScope.USER.value, user_id=ada.id, text="prefers tea", created_by_actor_id=ada.id
        )
        s.add(mem)
        await s.commit()
        beta = await _also_in(s, ada)

        _act_in(s, beta)
        assert await Memory.for_scope(s, MemoryScope.USER, user_id=ada.id) == []
        assert await Memory.search(s, MemoryScope.USER, query_text="tea", user_id=ada.id) == []
        assert await Memory.active_count(s, MemoryScope.USER, user_id=ada.id) == 0
        with pytest.raises(NotFound):
            await memory_ops.get(s, ada, mem.id)

        _act_in(s, acme)
        assert [m.id for m in await Memory.for_scope(s, MemoryScope.USER, user_id=ada.id)] == [mem.id]
        assert [m.id for m in await Memory.search(s, MemoryScope.USER, query_text="tea", user_id=ada.id)] == [
            mem.id
        ]
        assert await Memory.active_count(s, MemoryScope.USER, user_id=ada.id) == 1
        assert (await memory_ops.get(s, ada, mem.id)).id == mem.id


# ── bot.py ────────────────────────────────────────────────────────────────────────────────────


async def test_bot_registry_reads_are_fenced_to_the_bound_workspace() -> None:
    async with session_scope() as s:
        ada, acme = await member(s, "Ada")
        head = BotRecord(
            name="fence-reviewer", version=1, status=BotStatus.PUBLISHED.value, owner_actor_id=ada.id
        )
        draft = BotRecord(
            name="fence-reviewer", version=2, status=BotStatus.DRAFT.value, owner_actor_id=ada.id
        )
        s.add_all([head, draft])
        await s.commit()
        beta = await _also_in(s, ada)

        _act_in(s, beta)
        assert await BotRecord.listing(s, project_id=None, owner_actor_id=ada.id) == []
        assert await BotRecord.all_versions(s, "fence-reviewer", project_id=None, owner_actor_id=ada.id) == []
        assert (
            await BotRecord.at_version(s, "fence-reviewer", 1, project_id=None, owner_actor_id=ada.id) is None
        )
        assert await BotRecord.head(s, "fence-reviewer", project_id=None, owner_actor_id=ada.id) is None
        assert await BotRecord.latest_draft(s, "fence-reviewer", ada.id, project_id=None) is None
        assert "fence-reviewer" not in {
            b.name for b in await BotRecord.published_heads(s, project_id=None, owner_actor_id=ada.id)
        }
        # The same personal name is free here: a bot is per (workspace, owner, name).
        s.add(
            BotRecord(name="fence-reviewer", version=1, status=BotStatus.DRAFT.value, owner_actor_id=ada.id)
        )
        await s.commit()

        _act_in(s, acme)
        assert {b.id for b in await BotRecord.listing(s, project_id=None, owner_actor_id=ada.id)} == {
            head.id,
            draft.id,
        }
        assert (
            len(await BotRecord.all_versions(s, "fence-reviewer", project_id=None, owner_actor_id=ada.id))
            == 2
        )
        assert (
            await BotRecord.at_version(s, "fence-reviewer", 1, project_id=None, owner_actor_id=ada.id)
        ).id == head.id  # pyright: ignore[reportOptionalMemberAccess]
        assert (
            await BotRecord.head(s, "fence-reviewer", project_id=None, owner_actor_id=ada.id)
        ).id == head.id  # pyright: ignore[reportOptionalMemberAccess]
        assert (await BotRecord.latest_draft(s, "fence-reviewer", ada.id, project_id=None)).id == draft.id  # pyright: ignore[reportOptionalMemberAccess]
        assert "fence-reviewer" in {
            b.name for b in await BotRecord.published_heads(s, project_id=None, owner_actor_id=ada.id)
        }


async def test_the_roster_never_carries_another_owners_personal_bot() -> None:
    """Two people in ONE workspace, each with a personal bot of the same name: each roster holds only
    its own - whether the other owner's version is equal (the head join matches on name/version/
    project, so the scope must hold on the outer rows) or HIGHER (the scope must hold inside the
    max, or the other owner's v2 would hide this owner's v1)."""
    async with session_scope() as s:
        ada, _acme = await member(s, "Ada")
        bo, _ = await member(s, "Bo")
        mine = BotRecord(
            name="fence-twin", version=1, status=BotStatus.PUBLISHED.value, owner_actor_id=ada.id
        )
        theirs = BotRecord(
            name="fence-twin", version=1, status=BotStatus.PUBLISHED.value, owner_actor_id=bo.id
        )
        s.add_all([mine, theirs])
        await s.commit()

        roster = await BotRecord.published_heads(s, project_id=None, owner_actor_id=ada.id)
        assert [b.id for b in roster if b.name == "fence-twin"] == [mine.id]

        higher = BotRecord(
            name="fence-twin", version=2, status=BotStatus.PUBLISHED.value, owner_actor_id=bo.id
        )
        s.add(higher)
        await s.commit()
        roster = await BotRecord.published_heads(s, project_id=None, owner_actor_id=ada.id)
        assert [b.id for b in roster if b.name == "fence-twin"] == [mine.id]  # still hers, never Bo's
        bos = await BotRecord.published_heads(s, project_id=None, owner_actor_id=bo.id)
        assert [b.id for b in bos if b.name == "fence-twin"] == [higher.id]


# ── bot_channel.py ────────────────────────────────────────────────────────────────────────────


async def test_bot_channel_reads_are_fenced_to_the_bound_workspace() -> None:
    async with session_scope() as s:
        ada, acme = await member(s, "Ada")
        chan = BotChannel(owner_actor_id=ada.id, bot_name="fence-bot", kind="webhook")
        s.add(chan)
        await s.commit()
        beta = await _also_in(s, ada)

        _act_in(s, beta)
        assert await BotChannel.for_owner(s, ada.id) == []
        assert await BotChannel.for_bot(s, ada.id, "fence-bot") == []
        with pytest.raises(NotFound):
            await channel_ops.get_channel(s, actor=ada, channel_id=chan.id)

        _act_in(s, acme)
        assert [c.id for c in await BotChannel.for_owner(s, ada.id)] == [chan.id]
        assert [c.id for c in await BotChannel.for_bot(s, ada.id, "fence-bot")] == [chan.id]
        assert (await channel_ops.get_channel(s, actor=ada, channel_id=chan.id)).id == chan.id


# ── plugin_install.py, bot_template_install.py, api_key.py ────────────────────────────────────


async def test_plugin_install_list_is_fenced_to_the_bound_workspace() -> None:
    async with session_scope() as s:
        ada, acme = await member(s, "Ada")
        row = PluginInstall(owner_actor_id=ada.id, name="fence-plugin")
        s.add(row)
        await s.commit()
        beta = await _also_in(s, ada)

        _act_in(s, beta)
        assert await PluginInstall.for_owner(s, ada.id) == []
        _act_in(s, acme)
        assert [r.id for r in await PluginInstall.for_owner(s, ada.id)] == [row.id]


async def test_bot_template_install_list_is_fenced_to_the_bound_workspace() -> None:
    async with session_scope() as s:
        ada, acme = await member(s, "Ada")
        template = BotTemplate(
            share_slug=f"fence-{uuid.uuid4().hex[:10]}", name="fence-bot", bot_md="---\n---\n"
        )
        s.add(template)
        await s.flush()
        row = BotTemplateInstall(template_id=template.id, installer_actor_id=ada.id, bot_name="fence-bot")
        s.add(row)
        await s.commit()
        beta = await _also_in(s, ada)

        _act_in(s, beta)
        assert await BotTemplateInstall.for_installer(s, ada.id) == []
        _act_in(s, acme)
        assert [r.id for r in await BotTemplateInstall.for_installer(s, ada.id)] == [row.id]
        await s.delete(template)  # a platform row: the scratch fixture does not sweep templates
        await s.commit()


_TEMPLATE_MD = """---
name: fence-scout
description: >
  Reviews vendor contracts against our standard terms. Load when: an MSA or SOW needs a
  clause-level read. Not for: drafting contracts.
capability: frontier-reasoner
produces: document
tools: [frame.read_file]
---
Read every clause. Compare it to the standard terms. Flag each deviation.
"""


async def test_a_template_install_lands_in_the_bound_workspace_and_wires_only_its_connectors() -> None:
    async with session_scope() as s:
        ada, acme = await member(s, "Ada")
        s.add(
            Connector(
                owner_actor_id=ada.id, name="Linear", slug="linear", server_url="https://mcp.linear.app"
            )
        )
        template = BotTemplate(
            share_slug=f"fence-{uuid.uuid4().hex[:10]}",
            name="fence-scout",
            status="published",
            bot_md=_TEMPLATE_MD,
            cases_yaml="selection:\n  - goal: Review this MSA\n    expect: fence-scout\n",
            manifest={"connectors": ["linear"]},
        )
        s.add(template)
        await s.commit()
        beta = await _also_in(s, ada)

        _act_in(s, beta)
        record, outcome = await template_ops.install(s, actor=ada, slug=template.share_slug)
        assert record.workspace_id == beta.id  # the draft lands where the installer is acting
        assert outcome.needs_connect == ("linear",)  # acme's Linear is not beta's hands

        _act_in(s, acme)
        assert await BotRecord.all_versions(s, "fence-scout", project_id=None, owner_actor_id=ada.id) == []
        await s.delete(template)  # a platform row: the scratch fixture does not sweep templates
        await s.commit()


async def test_api_key_list_and_revoke_are_fenced_to_the_bound_workspace() -> None:
    async with session_scope() as s:
        ada, acme = await member(s, "Ada")
        key, _raw = ApiKey.issue(ada.id, "laptop", workspace_id=acme.id)
        s.add(key)
        await s.commit()
        beta = await _also_in(s, ada)

        _act_in(s, beta)
        assert await ApiKey.list_for(s, ada.id) == []
        assert await ApiKey.revoke(s, ada.id, key.id) is False  # another workspace's key is not there

        _act_in(s, acme)
        assert [k.id for k in await ApiKey.list_for(s, ada.id)] == [key.id]
        assert await ApiKey.revoke(s, ada.id, key.id) is True


# ── skill.py: the workspace's own versions and the builtins, never another workspace's ────────────────────

_MD = """---
description: Load when the user wants a fenced widget memo. Not for spreadsheets or code.
capability: fast-executor
---
Write the fenced widget memo.
"""
_CASES = "routing:\n  - goal: write a fenced widget memo\n    expect: {name}\n"


async def test_skill_resolution_reads_the_bound_workspace_and_the_builtins_only() -> None:
    name = f"fence{uuid.uuid4().hex[:8]}"
    async with session_scope() as s:
        ada, acme = await member(s, "Ada")
        draft = await skill_ops.push(
            s, actor_id=ada.id, name=name, skill_md=_MD, cases_yaml=_CASES.format(name=name)
        )
        await skill_ops.apply_eval(s, name, version=draft.version, green=True)
        await skill_ops.publish(s, actor_id=ada.id, name=name)
        builtin = await SkillRecord.head(s, "report")
        assert builtin is not None and builtin.workspace_id is None
        beta = await _also_in(s, ada)

        _act_in(s, beta)
        assert await SkillRecord.head(s, name) is None
        assert await SkillRecord.at_version(s, name, draft.version) is None
        assert await SkillRecord.all_versions(s, name) == []
        with pytest.raises(NotFound):
            await skill_ops.resolve_ref(s, name, actor_id=ada.id)
        heads = {h.name for h in await SkillRecord.published_heads(s)}
        assert name not in heads and "report" in heads
        assert (await SkillRecord.head(s, "report")).id == builtin.id  # pyright: ignore[reportOptionalMemberAccess]

        _act_in(s, acme)
        assert (await SkillRecord.head(s, name)).id == draft.id  # pyright: ignore[reportOptionalMemberAccess]
        assert (await skill_ops.resolve_ref(s, name, actor_id=ada.id)).version == draft.version
        heads = {h.name for h in await SkillRecord.published_heads(s)}
        assert name in heads and "report" in heads


async def test_a_draft_of_another_workspace_does_not_resolve_for_its_author() -> None:
    name = f"fence{uuid.uuid4().hex[:8]}"
    async with session_scope() as s:
        ada, acme = await member(s, "Ada")
        await skill_ops.push(s, actor_id=ada.id, name=name, skill_md=_MD, cases_yaml=_CASES.format(name=name))
        beta = await _also_in(s, ada)

        _act_in(s, beta)
        assert await SkillRecord.latest_draft(s, name, ada.id) is None
        with pytest.raises(NotFound):
            await skill_ops.resolve_ref(s, f"{name}@draft", actor_id=ada.id)
        _act_in(s, acme)
        assert await SkillRecord.latest_draft(s, name, ada.id) is not None


async def test_a_builtins_name_is_a_409_to_draft_or_publish() -> None:
    async with session_scope() as s:
        ada, _acme = await member(s, "Ada")
        with pytest.raises(Conflict, match="that name belongs to a built-in skill"):
            await skill_ops.push(
                s, actor_id=ada.id, name="report", skill_md=_MD, cases_yaml=_CASES.format(name="report")
            )
        # A draft under a builtin's name that predates the rule (a pre-0b row) cannot publish.
        s.add(
            SkillRecord(
                name="report",
                version=900,
                status=SkillStatus.DRAFT.value,
                origin=SkillOrigin.USER.value,
                eval_status="green",
                created_by=ada.id,
                workspace_id=tenant.workspace_of(s),
            )
        )
        await s.commit()
        with pytest.raises(Conflict, match="that name belongs to a built-in skill"):
            await skill_ops.publish(s, actor_id=ada.id, name="report")
