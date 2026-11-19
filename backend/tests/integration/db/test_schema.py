# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The roster, asserted against the database that actually exists.

The failure this prevents is specific and nasty: a model that is written but never imported into
`db/models/__init__.py` is invisible to Alembic's autogenerate, so the migration is generated
*without it* and nothing complains - until production reads a table that was never created. This
test compares three things that must agree: the roster (`ALL_TABLES`), the SQLAlchemy metadata,
and `information_schema` in the migrated database.

Needs the stack: `just up && just migrate`.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from prismoid.db.base import Base
from prismoid.db.engine import get_engine
from prismoid.db.models import ALL_TABLES

pytestmark = pytest.mark.integration


async def live_tables() -> set[str]:
    async with get_engine().connect() as conn:
        rows = await conn.execute(
            text(
                "select table_name from information_schema.tables "
                "where table_schema = 'public' and table_type = 'BASE TABLE'"
            )
        )
        return {r[0] for r in rows}


async def test_every_table_in_the_roster_exists_in_the_database() -> None:
    live = await live_tables()
    missing = set(ALL_TABLES) - live
    assert not missing, f"declared in ALL_TABLES but not migrated: {sorted(missing)}"


async def test_every_migrated_table_is_in_the_roster() -> None:
    """The other direction. A table nobody declared is a table nobody owns."""
    live = await live_tables()
    ours = live - {"alembic_version"} - {t for t in live if t.startswith("dbos")}
    extra = ours - set(ALL_TABLES)
    assert not extra, f"in the database but not in ALL_TABLES: {sorted(extra)}"


def test_metadata_and_roster_agree() -> None:
    """And the third leg: SQLAlchemy's metadata (what autogenerate diffs against)."""
    assert set(Base.metadata.tables) == set(ALL_TABLES)


async def test_artifacts_are_never_parented_to_a_project() -> None:
    """An artifact belongs to its Task, and to a Project only *through* it. A second
    parent is a second truth. A rule about what must NOT exist can only be validated by asking the
    live schema whether it does - so we do."""
    async with get_engine().connect() as conn:
        cols = {
            r[0]
            for r in await conn.execute(
                text("select column_name from information_schema.columns where table_name = 'artifacts'")
            )
        }
        fks = {
            r[0]
            for r in await conn.execute(
                text(
                    "select k.column_name from information_schema.table_constraints tc "
                    "join information_schema.key_column_usage k using (constraint_name) "
                    "where tc.table_name = 'artifacts' and tc.constraint_type = 'FOREIGN KEY'"
                )
            )
        }
    assert "project_id" not in cols, "artifacts grew a project_id column - a second parent is a second truth"
    # supersedes_id: the revision chain's one-step-back pointer - self-referential,
    # SET NULL, and NOT a second parent: lineage_id deliberately carries no FK at all.
    assert fks == {"task_id", "step_id", "supersedes_id"}, f"artifacts' FK set drifted: {sorted(fks)}"


async def test_the_ledger_carries_its_own_project_id() -> None:
    """The ledger law: usage is attributed at time of spend and never rewritten.

    That is only possible because `usage_events.project_id` is a real column. If it were derived by
    joining through `tasks`, moving a task between projects would silently rewrite every historical
    row - and the invoice for March would change in April.
    """
    async with get_engine().connect() as conn:
        rows = await conn.execute(
            text("select column_name from information_schema.columns where table_name = 'usage_events'")
        )
        cols = {r[0] for r in rows}
    assert {"project_id", "task_id", "cost_usd", "price_version"} <= cols


async def test_money_is_numeric_not_float() -> None:
    """The ledger reconciles to the cent. A double precision column cannot promise that."""
    async with get_engine().connect() as conn:
        rows = await conn.execute(
            text(
                "select table_name, column_name, data_type from information_schema.columns "
                "where column_name in ('cost_usd', 'usd_per_unit', 'budget_usd')"
            )
        )
        found = [(r[0], r[1], r[2]) for r in rows]
    assert found, "no money columns found - did the ledger tables migrate?"
    for table, column, data_type in found:
        assert data_type == "numeric", f"{table}.{column} is {data_type}, not numeric"
