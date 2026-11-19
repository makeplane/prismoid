# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Every vocabulary column's CHECK in the database names exactly its enum's words.

The models build each `ck_<table>_<column>_vocabulary` from its `StrEnum` (`db/base.vocabulary`);
the migrations froze the word lists the day they ran. A word added to an enum without a migration
that replaces its constraint would be refused by Postgres on first write, so this test compares the
two: the population is every vocabulary constraint in the model metadata, never a hand list.
"""

from __future__ import annotations

import re
import uuid

import pytest
from sqlalchemy import CheckConstraint, text
from sqlalchemy.exc import IntegrityError

from prismoid.db.base import Base
from prismoid.db.models import tenant
from prismoid.db.models.task import Task
from prismoid.db.session import session_scope
from tests.support.cast import member

pytestmark = pytest.mark.integration

WORD = re.compile(r"'([^']*)'")


def _declared() -> dict[str, set[str]]:
    """Each vocabulary constraint in the metadata, by name, with the words its enum allows."""
    out: dict[str, set[str]] = {}
    for table in Base.metadata.tables.values():
        for constraint in table.constraints:
            if isinstance(constraint, CheckConstraint) and str(constraint.name).endswith("_vocabulary"):
                out[str(constraint.name)] = set(WORD.findall(str(constraint.sqltext)))
    return out


async def test_the_database_checks_match_the_enums() -> None:
    declared = _declared()
    assert declared, "no vocabulary constraint in the metadata - the population is empty"
    async with session_scope() as session:
        tenant.bind_platform(session)
        rows = await session.execute(
            text(
                "select conname, pg_get_constraintdef(oid) from pg_constraint"
                " where contype = 'c' and conname like 'ck_%_vocabulary'"
            )
        )
        live = {name: set(WORD.findall(definition)) for name, definition in rows.tuples().all()}
    assert set(live) == set(declared), "a vocabulary constraint is in the models or the database only"
    drift = {name: (declared[name], live[name]) for name in declared if declared[name] != live[name]}
    assert not drift, f"enum words without a migration (models, database): {drift}"


async def test_a_word_outside_the_enum_is_refused() -> None:
    async with session_scope() as session:
        actor, _ = await member(session, "Strict")
        session.add(
            Task(
                id=uuid.uuid4(),
                goal="g",
                creator_actor_id=actor.id,
                criteria={},
                inputs=[],
                status="finished",
            )
        )
        with pytest.raises(IntegrityError, match="ck_tasks_status_vocabulary"):
            await session.flush()
