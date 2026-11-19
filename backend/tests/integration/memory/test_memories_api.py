# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The context ledger's contract: lanes are walls, removal is retire, retrieval ranks.

- user rows are private - another user gets 404s and empty lists, never a 403 (existence is
  privileged);
- project rows follow the role matrix - viewers read, editors write, strangers see nothing;
- skill notes are platform-wide to read, author-only to change;
- nothing is ever deleted: `retire` flips status and the row stays for the lineage view;
- FTS search is recall-biased but rank-ordered - two matched terms beat one.
"""

from __future__ import annotations

import datetime as dt

import pytest

from prismoid.db.models.memory import Memory, MemoryKind, MemoryScope, MemoryStatus
from prismoid.db.models.project_member import ProjectMember
from prismoid.db.session import session_scope
from prismoid.errors import Forbidden, NotFound, ValidationFailed
from prismoid.operations import memory as ops
from prismoid.operations import projects as project_ops
from tests.support.cast import member

pytestmark = pytest.mark.integration


async def test_user_scope_is_private_and_retire_keeps_the_row() -> None:
    async with session_scope() as session:
        alice, _ = await member(session, "Alice")
        bob, _ = await member(session, "Bob")
        await session.flush()

        row = await ops.create(
            session,
            alice,
            scope=MemoryScope.USER,
            kind=MemoryKind.PREFERENCE,
            text="  Prefers concise memos.  ",
        )
        assert row.text == "Prefers concise memos.", "stored trimmed"
        assert row.user_id == alice.id and row.created_by_actor_id == alice.id

        mine = await ops.list_memories(session, alice, scope=MemoryScope.USER)
        assert [m.id for m in mine] == [row.id]
        assert await ops.list_memories(session, bob, scope=MemoryScope.USER) == [], (
            "another user's ledger never shows my rows"
        )
        with pytest.raises(NotFound):
            await ops.get(session, bob, row.id)  # a 404, not a 403 - existence is privileged
        with pytest.raises(NotFound):
            await ops.retire(session, bob, row.id)

        edited = await ops.update(session, alice, row.id, text="Prefers one-page memos.")
        assert edited.text == "Prefers one-page memos."

        retired = await ops.retire(session, alice, row.id)
        assert retired.status == MemoryStatus.RETIRED.value
        assert await ops.list_memories(session, alice, scope=MemoryScope.USER) == [], "active list hides it"
        lineage = await ops.list_memories(session, alice, scope=MemoryScope.USER, status=None)
        assert [m.id for m in lineage] == [row.id], "retire keeps the row - nothing is deleted"

        with pytest.raises(ValidationFailed):
            await ops.create(session, alice, scope=MemoryScope.USER, kind=MemoryKind.NOTE, text="   ")


async def test_project_scope_follows_the_role_matrix() -> None:
    async with session_scope() as session:
        owner, editor, viewer, stranger = [
            (await member(session, n))[0] for n in ("Own", "Edit", "View", "Out")
        ]
        await session.flush()
        project = (await project_ops.create(session, owner, name="ledger-probe")).project
        session.add_all(
            [
                ProjectMember(project_id=project.id, actor_id=editor.id, role="editor", added_by=owner.id),
                ProjectMember(project_id=project.id, actor_id=viewer.id, role="viewer", added_by=owner.id),
            ]
        )
        await session.flush()

        row = await ops.create(
            session,
            editor,
            scope=MemoryScope.PROJECT,
            kind=MemoryKind.NOTE,
            text="Numbers tables, not prose.",
            project_id=project.id,
        )
        with pytest.raises(Forbidden):
            await ops.create(
                session,
                viewer,
                scope=MemoryScope.PROJECT,
                kind=MemoryKind.NOTE,
                text="viewer cannot write",
                project_id=project.id,
            )
        with pytest.raises(NotFound):
            await ops.create(
                session,
                stranger,
                scope=MemoryScope.PROJECT,
                kind=MemoryKind.NOTE,
                text="stranger sees nothing",
                project_id=project.id,
            )
        with pytest.raises(ValidationFailed):
            await ops.create(
                session, editor, scope=MemoryScope.PROJECT, kind=MemoryKind.NOTE, text="no target"
            )

        seen = await ops.list_memories(session, viewer, scope=MemoryScope.PROJECT, project_id=project.id)
        assert [m.id for m in seen] == [row.id], "viewers read the shared ledger"
        with pytest.raises(NotFound):
            await ops.list_memories(session, stranger, scope=MemoryScope.PROJECT, project_id=project.id)

        # The shared ledger is the editors' voice: the OWNER may edit the editor's row, any
        # editor may retire it - authorship stays on the row, authority follows the role.
        await ops.update(session, owner, row.id, text="Numbers tables, never prose.")
        with pytest.raises(Forbidden):
            await ops.retire(session, viewer, row.id)
        retired = await ops.retire(session, editor, row.id)
        assert retired.status == MemoryStatus.RETIRED.value
        assert retired.created_by_actor_id == editor.id, "authorship survives every mutation"


async def test_skill_notes_read_wide_write_author_only() -> None:
    async with session_scope() as session:
        author, _ = await member(session, "Author")
        reader, _ = await member(session, "Reader")
        await session.flush()

        row = await ops.create(
            session,
            author,
            scope=MemoryScope.SKILL,
            kind=MemoryKind.INSTRUCTION,
            text="report: this workspace wants numbers tables.",
            skill_name="report",
        )
        seen = await ops.list_memories(session, reader, scope=MemoryScope.SKILL, skill_name="report")
        assert [m.id for m in seen] == [row.id], "skill notes are platform-wide"
        assert (await ops.get(session, reader, row.id)).id == row.id

        with pytest.raises(Forbidden):
            await ops.update(session, reader, row.id, text="hijacked")
        with pytest.raises(Forbidden):
            await ops.retire(session, reader, row.id)
        await ops.retire(session, author, row.id)


async def test_keyset_pages_ilike_narrowing_and_fts_rank() -> None:
    async with session_scope() as session:
        actor, _ = await member(session, "Pager")
        await session.flush()

        texts = [
            "Prefers concise executive memos",
            "Deploy only on Tuesdays",
            "Concise weekly newsletter format",
            "Cite sources with 100% coverage",
        ]
        rows = [
            await ops.create(session, actor, scope=MemoryScope.USER, kind=MemoryKind.NOTE, text=t)
            for t in texts
        ]
        base = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
        for i, row in enumerate(rows):
            row.created_at = base + dt.timedelta(seconds=i)
        await session.flush()

        first = await ops.list_memories(session, actor, scope=MemoryScope.USER, limit=2)
        assert [m.text for m in first] == [texts[3], texts[2]], "newest first"
        second = await ops.list_memories(
            session, actor, scope=MemoryScope.USER, limit=2, cursor=first[-1].cursor()
        )
        assert [m.text for m in second] == [texts[1], texts[0]], "keyset continues without overlap"

        hits = await ops.list_memories(session, actor, scope=MemoryScope.USER, q="100%")
        assert [m.text for m in hits] == [texts[3]], "a literal % in q matches itself"
        assert await ops.list_memories(session, actor, scope=MemoryScope.USER, q="100_") == [], (
            "_ does not wildcard-match the % - user-typed wildcards are literals"
        )

        ranked = await Memory.search(
            session,
            MemoryScope.USER,
            user_id=actor.id,
            query_text="write a concise memo for the board",
        )
        assert [m.text for m in ranked] == [texts[0], texts[2]], (
            "two matched terms (concise+memo) outrank one (concise); non-matches never appear"
        )


async def test_over_cap_victims_choose_least_recently_used_first() -> None:
    async with session_scope() as session:
        actor, _ = await member(session, "Capper")
        await session.flush()

        rows = [
            await ops.create(session, actor, scope=MemoryScope.USER, kind=MemoryKind.NOTE, text=f"note {i}")
            for i in range(3)
        ]
        base = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
        for i, row in enumerate(rows):
            row.created_at = base + dt.timedelta(seconds=i)
        rows[0].last_used_at = base + dt.timedelta(days=2)  # oldest-made but recently used - safe
        await session.flush()

        victims = await Memory.over_cap_victims(session, MemoryScope.USER, user_id=actor.id, cap=2)
        assert [v.id for v in victims] == [rows[1].id], (
            "never-used counts as oldest; the recently-used row survives despite its age"
        )
        assert await Memory.over_cap_victims(session, MemoryScope.USER, user_id=actor.id, cap=3) == []


async def test_bot_scope_wears_its_carriers_matrix() -> None:
    """The bot lane: a personal bot's memory is the owner's alone (strangers 404
    on the bot itself); a project bot's memory wears the project matrix verbatim - viewers read,
    editors write. The bot must exist in the named scope, so a typo is a 404, not an orphan."""
    from prismoid.operations import bots as bot_ops
    from prismoid.schemas.bots import BotCase, BotSource

    def source(name: str) -> BotSource:
        return BotSource(
            job="Reviews vendor contracts",
            use_when=("an MSA needs a close read",),
            not_for=("legal advice",),
            instructions="Review every clause closely.",
            cases=(BotCase(goal="Review this MSA", expect=name),),
        )

    async with session_scope() as session:
        owner, editor, viewer, stranger = [
            (await member(session, n))[0] for n in ("Own", "Edit", "View", "Out")
        ]
        await session.flush()

        # Personal: the owner's alone.
        await bot_ops.push(session, actor=owner, name="reviewer", source=source("reviewer"))
        row = await ops.create(
            session,
            owner,
            scope=MemoryScope.BOT,
            kind=MemoryKind.FACT,
            text="The Q3 workbook lives in the finance project.",
            bot_name="reviewer",
        )
        assert row.bot_name == "reviewer" and row.user_id == owner.id and row.project_id is None
        with pytest.raises(NotFound):  # no bot of that name in the stranger's personal scope
            await ops.list_memories(session, stranger, scope=MemoryScope.BOT, bot_name="reviewer")
        with pytest.raises(NotFound):  # the row itself 404s a stranger, like a user row
            await ops.get(session, stranger, row.id)
        with pytest.raises(ValidationFailed):  # name required
            await ops.create(session, owner, scope=MemoryScope.BOT, kind=MemoryKind.NOTE, text="x")

        # Project: the project matrix, on the bot's carrier.
        project = (await project_ops.create(session, owner, name="bot-ledger-probe")).project
        session.add_all(
            [
                ProjectMember(project_id=project.id, actor_id=editor.id, role="editor", added_by=owner.id),
                ProjectMember(project_id=project.id, actor_id=viewer.id, role="viewer", added_by=owner.id),
            ]
        )
        await session.flush()
        await bot_ops.push(
            session, actor=owner, name="scribe", source=source("scribe"), project_id=project.id
        )

        shared = await ops.create(
            session,
            editor,
            scope=MemoryScope.BOT,
            kind=MemoryKind.PREFERENCE,
            text="Dana wants auto-renewal clauses flagged.",
            bot_name="scribe",
            project_id=project.id,
        )
        assert shared.bot_name == "scribe" and shared.project_id == project.id and shared.user_id is None
        with pytest.raises(Forbidden):  # viewers read, never write
            await ops.create(
                session,
                viewer,
                scope=MemoryScope.BOT,
                kind=MemoryKind.NOTE,
                text="viewer cannot write",
                bot_name="scribe",
                project_id=project.id,
            )
        seen = await ops.list_memories(
            session, viewer, scope=MemoryScope.BOT, bot_name="scribe", project_id=project.id
        )
        assert [m.id for m in seen] == [shared.id]
        with pytest.raises(NotFound):
            await ops.list_memories(
                session, stranger, scope=MemoryScope.BOT, bot_name="scribe", project_id=project.id
            )
        retired = await ops.retire(session, editor, shared.id)
        assert retired.status == MemoryStatus.RETIRED.value

        # The lanes stay walled: the personal reviewer lane never shows the project rows.
        mine = await ops.list_memories(session, owner, scope=MemoryScope.BOT, bot_name="reviewer")
        assert [m.id for m in mine] == [row.id]
