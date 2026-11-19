# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The bot registry: the publish gate, author-scoped drafts, and the Project role matrix.

These are the refusals - the part that protects the roster. The model is out of the loop
throughout: `apply_eval` writes the verdict directly, so what is measured here is the gate, not
the Lead. Its routing of each bot's cases is `just evals --suite bot-selection`'s job.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models.actor import Actor
from prismoid.db.models.bot import BotStatus
from prismoid.db.models.project import Project, Role
from prismoid.db.models.project_member import ProjectMember
from prismoid.db.session import session_scope
from prismoid.errors import Conflict, Forbidden, NotFound, ValidationFailed
from prismoid.operations import bots as ops
from prismoid.schemas.bots import BotSource, SelectionCaseResult, SelectionReport
from tests.support.cast import member

pytestmark = pytest.mark.integration

DESCRIPTION = (
    "Reviews vendor contracts against our standard terms. "
    "Load when: an MSA or SOW needs a clause-level read. Not for: drafting contracts."
)
INSTRUCTIONS = "Read every clause. Compare it to the standard terms. Flag each deviation."
BOT_MD = f"""---
name: {{name}}
description: >
  {DESCRIPTION}
capability: frontier-reasoner
produces: document
tools: [frame.read_file]
---
{INSTRUCTIONS}
"""


def _source(name: str) -> BotSource:
    return BotSource(
        bot_md=BOT_MD.format(name=name),
        cases_yaml=f"selection:\n  - goal: Review this MSA\n    expect: {name}\n",
    )


def _report(name: str, version: int, *, green: bool) -> SelectionReport:
    return SelectionReport(
        bot=name,
        version=version,
        passed=1 if green else 0,
        total=1,
        cases=(
            SelectionCaseResult(
                goal="Review this MSA", expected=name, chosen=name if green else "none", ok=green
            ),
        ),
    )


async def _an_actor(session: AsyncSession, name: str = "Author") -> Actor:
    return (await member(session, name))[0]


async def _a_project(session: object, owner: Actor) -> Project:
    row = Project(name="Legal", owner_actor_id=owner.id)
    session.add(row)  # pyright: ignore[reportAttributeAccessIssue]
    await session.flush()  # pyright: ignore[reportAttributeAccessIssue]
    return row


# ── the publish gate ──────────────────────────────────────────────────────────────────────────


async def test_publish_a_green_draft_promotes_it() -> None:
    async with session_scope() as session:
        author = await _an_actor(session)
        name = f"bot-green-{uuid.uuid4().hex[:8]}"
        draft = await ops.push(session, actor=author, name=name, source=_source(name))
        await ops.apply_eval(
            session,
            actor=author,
            name=name,
            version=draft.version,
            report=_report(name, draft.version, green=True),
        )
        published = await ops.publish(session, actor=author, name=name)
        assert published.status == BotStatus.PUBLISHED.value


async def test_publish_a_red_draft_is_a_409() -> None:
    async with session_scope() as session:
        author = await _an_actor(session)
        name = f"bot-red-{uuid.uuid4().hex[:8]}"
        draft = await ops.push(session, actor=author, name=name, source=_source(name))
        await ops.apply_eval(
            session,
            actor=author,
            name=name,
            version=draft.version,
            report=_report(name, draft.version, green=False),
        )
        with pytest.raises(Conflict, match="not green"):
            await ops.publish(session, actor=author, name=name)


async def test_publish_an_untested_draft_is_a_409() -> None:
    """The default is untested, and untested is not green - a bot nobody scored is a bot whose
    description nobody has checked against the rest of the roster."""
    async with session_scope() as session:
        author = await _an_actor(session)
        name = f"bot-untested-{uuid.uuid4().hex[:8]}"
        await ops.push(session, actor=author, name=name, source=_source(name))
        with pytest.raises(Conflict, match="untested"):
            await ops.publish(session, actor=author, name=name)


async def test_a_published_version_is_frozen_by_a_later_verdict() -> None:
    """Re-scoring a published bot against a roster that has since changed would rewrite history
    to explain the present. Only a draft's status moves."""
    async with session_scope() as session:
        author = await _an_actor(session)
        name = f"bot-frozen-{uuid.uuid4().hex[:8]}"
        draft = await ops.push(session, actor=author, name=name, source=_source(name))
        await ops.apply_eval(
            session,
            actor=author,
            name=name,
            version=draft.version,
            report=_report(name, draft.version, green=True),
        )
        published = await ops.publish(session, actor=author, name=name)
        await ops.apply_eval(
            session,
            actor=author,
            name=name,
            version=published.version,
            report=_report(name, published.version, green=False),
        )
        await session.refresh(published)
        assert published.eval_status == "green"
        assert published.status == BotStatus.PUBLISHED.value


# ── versions and pins ─────────────────────────────────────────────────────────────────────────


async def test_every_push_is_a_new_version() -> None:
    async with session_scope() as session:
        author = await _an_actor(session)
        name = f"bot-versions-{uuid.uuid4().hex[:8]}"
        first = await ops.push(session, actor=author, name=name, source=_source(name))
        second = await ops.push(session, actor=author, name=name, source=_source(name))
        assert (first.version, second.version) == (1, 2)


async def test_a_pin_resolves_to_an_exact_version() -> None:
    """Reproducibility is the point: publishing v2 must not change what a running task is doing."""
    async with session_scope() as session:
        author = await _an_actor(session)
        name = f"bot-pin-{uuid.uuid4().hex[:8]}"
        draft = await ops.push(session, actor=author, name=name, source=_source(name))
        await ops.apply_eval(
            session,
            actor=author,
            name=name,
            version=draft.version,
            report=_report(name, draft.version, green=True),
        )
        await ops.publish(session, actor=author, name=name)
        await ops.push(session, actor=author, name=name, source=_source(name))  # v2, a draft

        assert (await ops.resolve_ref(session, name, actor=author)).version == 1  # head, not newest
        assert (await ops.resolve_ref(session, f"{name}@draft", actor=author)).version == 2
        assert (await ops.resolve_ref(session, f"{name}@1", actor=author)).version == 1


async def test_a_draft_resolves_only_for_its_author() -> None:
    """A second user's `@draft` finds nothing - a 404, not someone else's unfinished work."""
    async with session_scope() as session:
        author = await _an_actor(session, "Author")
        stranger = await _an_actor(session, "Stranger")
        name = f"bot-draft-{uuid.uuid4().hex[:8]}"
        await ops.push(session, actor=author, name=name, source=_source(name))
        with pytest.raises(NotFound, match=r"not yours|no draft"):
            await ops.resolve_ref(session, f"{name}@draft", actor=stranger)


async def test_a_numbered_pin_never_resolves_a_co_editors_draft() -> None:
    """A project bot's draft is its author's workbench: `name@N` from another member is the same
    404 `@draft` gives, while the author's own numbered pin still resolves."""
    async with session_scope() as session:
        owner = await _an_actor(session, "Owner")
        editor = await _an_actor(session, "Editor")
        project = await _a_project(session, owner)
        session.add(
            ProjectMember(
                project_id=project.id, actor_id=editor.id, role=Role.EDITOR.value, added_by=owner.id
            )
        )
        await session.flush()
        name = f"bot-draftnum-{uuid.uuid4().hex[:8]}"
        draft = await ops.push(session, actor=editor, name=name, source=_source(name), project_id=project.id)
        ref = f"{name}@{draft.version}"

        mine = await ops.resolve_ref(session, ref, actor=editor, project_id=project.id)
        assert mine.version == draft.version
        with pytest.raises(NotFound):
            await ops.resolve_ref(session, ref, actor=owner, project_id=project.id)


# ── the door ──────────────────────────────────────────────────────────────────────────────────


async def test_a_bot_may_not_take_a_built_in_name() -> None:
    """Refused here so an author gets a 422 they can act on; refused again at compile time so a
    built-in added later still wins."""
    async with session_scope() as session:
        author = await _an_actor(session)
        with pytest.raises(ValidationFailed, match="built-in"):
            await ops.push(session, actor=author, name="writer", source=_source("writer"))


async def test_a_malformed_bot_is_a_422_naming_the_field() -> None:
    async with session_scope() as session:
        author = await _an_actor(session)
        name = f"bot-bad-{uuid.uuid4().hex[:8]}"
        bad = BotSource(
            bot_md=BOT_MD.format(name=name).replace("Not for: drafting contracts.", ""), cases_yaml=""
        )
        with pytest.raises(ValidationFailed) as caught:
            await ops.push(session, actor=author, name=name, source=bad)
        assert "description" in caught.value.fields


async def test_the_structured_form_and_the_raw_form_agree() -> None:
    """The web sends fields, the CLI sends a file; both are rendered into the same bytes and walk
    through the same door, so they cannot drift."""
    async with session_scope() as session:
        author = await _an_actor(session)
        name = f"bot-struct-{uuid.uuid4().hex[:8]}"
        from prismoid.schemas.bots import BotCase

        structured = BotSource(
            description=DESCRIPTION,
            instructions=INSTRUCTIONS,
            capability="frontier-reasoner",
            produces="document",
            tools=("frame.read_file",),
            cases=(BotCase(goal="Review this MSA", expect=name),),
        )
        record = await ops.push(session, actor=author, name=name, source=structured)
        assert record.manifest["description"].strip() == DESCRIPTION
        assert record.manifest["capability"] == "frontier-reasoner"
        assert record.instructions == INSTRUCTIONS


# ── the Project role matrix ─────────────────────────────────────────────────────────────────────


async def test_a_stranger_cannot_see_a_project_bot() -> None:
    """404, not 403 - a stranger asking about your Project learns nothing, not even that it exists."""
    async with session_scope() as session:
        owner = await _an_actor(session, "Owner")
        stranger = await _an_actor(session, "Stranger")
        project = await _a_project(session, owner)
        name = f"bot-project-{uuid.uuid4().hex[:8]}"
        await ops.push(session, actor=owner, name=name, source=_source(name), project_id=project.id)
        with pytest.raises(NotFound):
            await ops.list_bots(session, stranger, project_id=project.id)


async def test_a_viewer_may_read_but_not_push() -> None:
    async with session_scope() as session:
        owner = await _an_actor(session, "Owner")
        viewer = await _an_actor(session, "Viewer")
        project = await _a_project(session, owner)
        session.add(
            ProjectMember(
                project_id=project.id, actor_id=viewer.id, role=Role.VIEWER.value, added_by=owner.id
            )
        )
        await session.flush()
        name = f"bot-viewer-{uuid.uuid4().hex[:8]}"
        await ops.push(session, actor=owner, name=name, source=_source(name), project_id=project.id)

        assert await ops.list_bots(session, viewer, project_id=project.id)  # reading is allowed
        with pytest.raises(Forbidden, match=r"^viewers watch; ask the Project owner for edit access$"):
            await ops.push(session, actor=viewer, name=name, source=_source(name), project_id=project.id)


async def test_an_editor_may_push_and_publish_their_own_draft() -> None:
    async with session_scope() as session:
        owner = await _an_actor(session, "Owner")
        editor = await _an_actor(session, "Editor")
        project = await _a_project(session, owner)
        session.add(
            ProjectMember(
                project_id=project.id, actor_id=editor.id, role=Role.EDITOR.value, added_by=owner.id
            )
        )
        await session.flush()
        name = f"bot-editor-{uuid.uuid4().hex[:8]}"
        draft = await ops.push(session, actor=editor, name=name, source=_source(name), project_id=project.id)
        await ops.apply_eval(
            session,
            actor=editor,
            name=name,
            version=draft.version,
            report=_report(name, draft.version, green=True),
            project_id=project.id,
        )
        published = await ops.publish(session, actor=editor, name=name, project_id=project.id)
        assert published.status == BotStatus.PUBLISHED.value


async def test_a_personal_bot_is_invisible_in_someone_elses_scope() -> None:
    """Two people may each have a `reviewer`, and neither sees the other's."""
    async with session_scope() as session:
        alice = await _an_actor(session, "Alice")
        bob = await _an_actor(session, "Bob")
        name = f"bot-mine-{uuid.uuid4().hex[:8]}"
        await ops.push(session, actor=alice, name=name, source=_source(name))
        await ops.push(session, actor=bob, name=name, source=_source(name))  # same name, no collision

        assert [r.name for r in await ops.versions(session, alice, name)] == [name]
        alice_rows = await ops.versions(session, alice, name)
        bob_rows = await ops.versions(session, bob, name)
        assert alice_rows[0].id != bob_rows[0].id


# ── the roster ────────────────────────────────────────────────────────────────────────────────


async def test_the_roster_is_published_bots_only() -> None:
    """An unpublished bot is not part of the roster. That is the whole point of the split."""
    async with session_scope() as session:
        author = await _an_actor(session)
        draft_only = f"bot-hidden-{uuid.uuid4().hex[:8]}"
        live = f"bot-live-{uuid.uuid4().hex[:8]}"
        await ops.push(session, actor=author, name=draft_only, source=_source(draft_only))
        row = await ops.push(session, actor=author, name=live, source=_source(live))
        await ops.apply_eval(
            session,
            actor=author,
            name=live,
            version=row.version,
            report=_report(live, row.version, green=True),
        )
        await ops.publish(session, actor=author, name=live)

        names = {b.name for b in await ops.roster_for_task(session, author)}
        assert live in names
        assert draft_only not in names


async def test_a_project_task_sees_the_project_bots_and_its_actors_own() -> None:
    """The union is deliberate: a bot you built for yourself should not vanish because you filed
    the task in a folder, and a folder's bots should not follow you out of it."""
    async with session_scope() as session:
        owner = await _an_actor(session, "Owner")
        project = await _a_project(session, owner)
        personal = f"bot-personal-{uuid.uuid4().hex[:8]}"
        shared = f"bot-shared-{uuid.uuid4().hex[:8]}"
        for name, project_id in ((personal, None), (shared, project.id)):
            row = await ops.push(session, actor=owner, name=name, source=_source(name), project_id=project_id)
            await ops.apply_eval(
                session,
                actor=owner,
                name=name,
                version=row.version,
                report=_report(name, row.version, green=True),
                project_id=project_id,
            )
            await ops.publish(session, actor=owner, name=name, project_id=project_id)

        in_project = {b.name for b in await ops.roster_for_task(session, owner, project_id=project.id)}
        assert {personal, shared} <= in_project
        no_project = {b.name for b in await ops.roster_for_task(session, owner)}
        assert personal in no_project and shared not in no_project


async def test_a_roster_bot_compiles_into_a_runnable_agent() -> None:
    """The seam, from the database: a stored row reaches the engine as an `AgentDef`."""
    from prismoid.ai.agents.bot_compile import compile_bot

    async with session_scope() as session:
        author = await _an_actor(session)
        name = f"bot-compile-{uuid.uuid4().hex[:8]}"
        row = await ops.push(session, actor=author, name=name, source=_source(name))
        await ops.apply_eval(
            session,
            actor=author,
            name=name,
            version=row.version,
            report=_report(name, row.version, green=True),
        )
        await ops.publish(session, actor=author, name=name)

        roster = await ops.roster_for_task(session, author)
        agent = compile_bot(next(b for b in roster if b.name == name))
        assert agent.plannable and agent.publishes_output
        # The author's words first and unchanged, then the system's statement of how it delivers
        # (a live run found that a bot with no publish grant otherwise goes looking for one).
        assert agent.prompt.startswith(INSTRUCTIONS)
        assert "no tool to call" in agent.prompt


# ── found in review: the publish gate's presence arms, the dials that travel ─────────────────


async def test_publish_names_what_a_newborn_is_still_missing() -> None:
    async with session_scope() as session:
        author = await _an_actor(session)
        name = f"newborn-{uuid.uuid4().hex[:6]}"
        await ops.push(
            session,
            actor=author,
            name=name,
            source=BotSource(bot_md=f"---\nname: {name}\n---\n", cases_yaml=""),
        )
        with pytest.raises(Conflict, match="no routing description yet"):
            await ops.publish(session, actor=author, name=name)
        # a description, still no instructions
        await ops.push(
            session,
            actor=author,
            name=name,
            source=BotSource(
                bot_md=f"---\nname: {name}\ndescription: >\n  {DESCRIPTION}\n---\n", cases_yaml=""
            ),
        )
        with pytest.raises(Conflict, match="no instructions yet"):
            await ops.publish(session, actor=author, name=name)
        # instructions, still no cases
        await ops.push(
            session, actor=author, name=name, source=BotSource(bot_md=BOT_MD.format(name=name), cases_yaml="")
        )
        with pytest.raises(Conflict, match="no selection cases yet"):
            await ops.publish(session, actor=author, name=name)


async def test_the_per_name_dials_survive_a_new_version() -> None:
    async with session_scope() as session:
        author = await _an_actor(session)
        name = f"dials-{uuid.uuid4().hex[:6]}"
        await ops.push(session, actor=author, name=name, source=_source(name))
        await ops.set_face(session, actor=author, name=name, work_mode="never", threads="one", title="Desk")
        v2 = await ops.push(session, actor=author, name=name, source=_source(name))
        assert (v2.work_mode, v2.threads, v2.title) == ("never", "one", "Desk")
