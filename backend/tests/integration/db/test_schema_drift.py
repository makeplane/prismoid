# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The models and the migrations describe the same schema, column by column.

`test_schema.py` proves the two agree on WHICH tables exist. This proves they agree on what is in
them: every column's presence, type and nullability, every index, unique constraint and foreign
key. Alembic's autogenerate comparison runs the models against the migrated database; a non-empty
diff means a model changed without a migration (the next `alembic revision --autogenerate` would
pick up someone else's forgotten change), or a migration changed the database behind a model's back.

Needs the stack: `just up && just migrate`.
"""

from __future__ import annotations

from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection

from prismoid.db.base import Base
from prismoid.db.engine import get_engine

pytestmark = pytest.mark.integration


def _ours(obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any) -> bool:
    """DBOS owns its own tables in this database; the app's schema is everything else."""
    table_name = name if type_ == "table" else obj.table.name
    return not str(table_name).startswith("dbos")


def _diff(sync_conn: Connection) -> list[Any]:
    context = MigrationContext.configure(
        sync_conn, opts={"compare_type": True, "include_object": _ours, "target_metadata": Base.metadata}
    )
    return compare_metadata(context, Base.metadata)


async def test_the_models_match_the_migrated_schema() -> None:
    async with get_engine().connect() as conn:
        diff = await conn.run_sync(_diff)
    assert not diff, "models and migrations disagree (autogenerate would emit):\n" + "\n".join(
        f"  {entry}" for entry in diff
    )
