# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The publish gate and `@draft` author-scoping.

Publishing needs green evals - an HTTP 409, not a review comment - and a draft belongs to its author:
`@draft` resolves to *your* latest draft and to nobody else's. These tests pin both boundaries with
the model out of the loop: `apply_eval` sets the verdict directly, so the gate is measured, not the
router. The live drill proved the happy path end to end; here we prove the *refusals*, which are the
part that protects the registry.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models.skill import SkillStatus
from prismoid.db.session import session_scope
from prismoid.errors import Conflict, NotFound
from prismoid.operations import skills as ops
from tests.support.cast import member

pytestmark = pytest.mark.integration

_MD = """---
description: Load when the user wants a pubtest thing. Not for spreadsheets or code.
capability: fast-executor
---
Do the pubtest thing.
"""
_CASES = "routing:\n  - goal: do a pubtest\n    expect: pubtest\n"


async def _an_actor(session: AsyncSession, name: str = "Publisher") -> uuid.UUID:
    return (await member(session, name))[0].id


async def test_publish_a_green_draft_promotes_it() -> None:
    async with session_scope() as session:
        author = await _an_actor(session)
        draft = await ops.push(session, actor_id=author, name="pubgreen", skill_md=_MD, cases_yaml=_CASES)
        await ops.apply_eval(session, "pubgreen", version=draft.version, green=True)
        published = await ops.publish(session, actor_id=author, name="pubgreen")
        status = published.status

    assert status == SkillStatus.PUBLISHED.value


async def test_publish_a_red_draft_is_a_409() -> None:
    async with session_scope() as session:
        author = await _an_actor(session)
        draft = await ops.push(session, actor_id=author, name="pubred", skill_md=_MD, cases_yaml=_CASES)
        await ops.apply_eval(session, "pubred", version=draft.version, green=False)
        with pytest.raises(Conflict, match="not green"):
            await ops.publish(session, actor_id=author, name="pubred")


async def test_publish_an_untested_draft_is_a_409() -> None:
    # A draft nobody tested is not green, and green is the gate - untested must not sneak through.
    async with session_scope() as session:
        author = await _an_actor(session)
        await ops.push(session, actor_id=author, name="pubuntested", skill_md=_MD, cases_yaml=_CASES)
        with pytest.raises(Conflict):
            await ops.publish(session, actor_id=author, name="pubuntested")


async def test_publish_with_no_draft_is_a_404() -> None:
    async with session_scope() as session:
        author = await _an_actor(session)
        with pytest.raises(NotFound):
            await ops.publish(session, actor_id=author, name="pubnodraft")  # never pushed by anyone


async def test_draft_ref_resolves_for_the_author_only() -> None:
    async with session_scope() as session:
        author = await _an_actor(session, "Author")
        other = await _an_actor(session, "Other")
        draft = await ops.push(session, actor_id=author, name="draftref", skill_md=_MD, cases_yaml=_CASES)

        mine = await ops.resolve_ref(session, "draftref@draft", actor_id=author)
        assert (str(mine.name), mine.version) == ("draftref", draft.version)

        # A second user's `@draft` finds nothing - the draft is not theirs (404, not a leak).
        with pytest.raises(NotFound):
            await ops.resolve_ref(session, "draftref@draft", actor_id=other)
        # And with no author in context at all, a draft pin is refused.
        with pytest.raises(NotFound):
            await ops.resolve_ref(session, "draftref@draft", actor_id=None)


async def test_a_numbered_pin_never_resolves_another_authors_draft() -> None:
    """`name@N` reaches the same draft `@draft` guards: only its author may pin it - a teammate
    who read the version list, and a context with no author at all, get the 404."""
    async with session_scope() as session:
        author = await _an_actor(session, "Author")
        other = await _an_actor(session, "Other")
        draft = await ops.push(session, actor_id=author, name="draftnum", skill_md=_MD, cases_yaml=_CASES)
        ref = f"draftnum@{draft.version}"

        assert (await ops.resolve_ref(session, ref, actor_id=author)).version == draft.version
        with pytest.raises(NotFound):
            await ops.resolve_ref(session, ref, actor_id=other)
        with pytest.raises(NotFound):
            await ops.resolve_ref(session, ref, actor_id=None)


async def test_a_resolved_pin_is_stable_across_a_publish() -> None:
    # The reproducibility guarantee: a ref resolved to a version stays that version. Two resolves of
    # "pubstable" straddling a publish must return the versions that were the head *at each moment*.
    async with session_scope() as session:
        author = await _an_actor(session)
        v1 = await ops.push(session, actor_id=author, name="pubstable", skill_md=_MD, cases_yaml=_CASES)
        await ops.apply_eval(session, "pubstable", version=v1.version, green=True)
        await ops.publish(session, actor_id=author, name="pubstable")
        before = await ops.resolve_ref(session, "pubstable")  # head is v1

        v2 = await ops.push(session, actor_id=author, name="pubstable", skill_md=_MD, cases_yaml=_CASES)
        await ops.apply_eval(session, "pubstable", version=v2.version, green=True)
        await ops.publish(session, actor_id=author, name="pubstable")
        after = await ops.resolve_ref(session, "pubstable")  # head is now v2

    assert before.version == 1
    assert after.version == 2  # a NEW resolve gets the new head; the old pin (before) still says v1
