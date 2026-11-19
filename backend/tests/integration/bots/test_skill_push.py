# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The draft lifecycle and write-time enforcement.

`push` is the author's door into the registry, and the doctrine is enforced *at* it: a body over
budget or a description missing its "Load when" is a `ValidationFailed` with `fields`, which the API
renders as a 422 that names the broken rule. These tests pin both directions - a good skill becomes a
draft version, a bad one is refused with a field a human can act on - and that a pushed skill's cases
round-trip back out through `resolve`, which is what makes `pull → edit → push` a loop and not a
one-way trip.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models import tenant
from prismoid.db.models.skill import EvalStatus, SkillOrigin, SkillStatus
from prismoid.db.session import session_scope
from prismoid.errors import ValidationFailed
from prismoid.operations import skills as ops
from tests.support.cast import member

pytestmark = pytest.mark.integration

_GOOD_MD = """---
description: Load when the user wants a widget report. Not for spreadsheets or code.
capability: fast-executor
---
Lead with the number the reader repeats in the next meeting. Then support it. Then stop.
"""
_CASES = "routing:\n  - goal: write a widget status report\n    expect: widgetreport\n"


async def _an_actor(session: AsyncSession) -> uuid.UUID:
    return (await member(session, "Pusher"))[0].id


async def test_push_creates_a_draft_version_attributed_to_its_author() -> None:
    async with session_scope() as session:
        actor_id = await _an_actor(session)
        record = await ops.push(
            session, actor_id=actor_id, name="widgetreport", skill_md=_GOOD_MD, cases_yaml=_CASES
        )
        version, status, origin, evals, created_by, workspace_id = (
            record.version,
            record.status,
            record.origin,
            record.eval_status,
            record.created_by,
            record.workspace_id,
        )
        bound = tenant.workspace_of(session)

    assert version == 1  # first version of a brand-new skill
    assert status == SkillStatus.DRAFT.value
    assert origin == SkillOrigin.USER.value
    assert evals == EvalStatus.UNTESTED.value  # a draft is untested until `test` says otherwise
    assert created_by == actor_id  # a draft resolves only for its author - attribution matters
    assert workspace_id == bound, (
        "an authored skill belongs to its author's workspace; only builtins are NULL"
    )


async def test_a_second_push_increments_the_version() -> None:
    async with session_scope() as session:
        actor_id = await _an_actor(session)
        first = await ops.push(
            session, actor_id=actor_id, name="widgetv", skill_md=_GOOD_MD, cases_yaml=_CASES
        )
        second = await ops.push(
            session, actor_id=actor_id, name="widgetv", skill_md=_GOOD_MD, cases_yaml=_CASES
        )
        assert (first.version, second.version) == (1, 2)


async def test_over_budget_body_is_refused_with_a_field() -> None:
    huge = "---\ndescription: Load when X. Not for Y.\n---\n" + ("pad " * 7000)  # ~7000 tokens
    async with session_scope() as session:
        actor_id = await _an_actor(session)
        with pytest.raises(ValidationFailed) as exc:
            await ops.push(session, actor_id=actor_id, name="toobig", skill_md=huge, cases_yaml=_CASES)
    assert "body" in exc.value.fields  # the 422 names the rule, not just "invalid"


async def test_missing_load_when_is_refused_with_a_field() -> None:
    bad = "---\ndescription: The user might want a thing. Not for other things.\n---\nsome body\n"
    async with session_scope() as session:
        actor_id = await _an_actor(session)
        with pytest.raises(ValidationFailed) as exc:
            await ops.push(session, actor_id=actor_id, name="noloadwhen", skill_md=bad, cases_yaml=_CASES)
    assert "description" in exc.value.fields


async def test_a_pushed_skills_cases_round_trip_through_resolve() -> None:
    async with session_scope() as session:
        actor_id = await _an_actor(session)
        await ops.push(session, actor_id=actor_id, name="widgetrt", skill_md=_GOOD_MD, cases_yaml=_CASES)
        # resolve reads the exact version we just wrote (v1), body + manifest + cases
        resolved = await ops.resolve(session, "widgetrt", version=1)
        goals = [c.goal for c in resolved.routing_cases]

    assert goals == ["write a widget status report"], "the cases must survive the round trip (§11)"
