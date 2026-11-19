# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The declarative base - one metadata, stable constraint names, typed columns.

The naming convention is not cosmetic: Alembic autogenerates against it, so without it every
migration invents a fresh random name for the same index and the diffs never settle
(migrations only, and they must be reversible).
"""

from __future__ import annotations

import datetime as dt
import uuid
from enum import StrEnum
from typing import Any, ClassVar, override

from sqlalchemy import CheckConstraint, DateTime, Dialect, MetaData, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.engine import CursorResult, Result
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator, TypeEngine

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Every row in the product descends from here. Each model opens with its glossary line."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map: ClassVar[dict[Any, TypeEngine[Any]]] = {
        uuid.UUID: PgUUID(as_uuid=True),
        dt.datetime: DateTime(timezone=True),  # tz-aware always; naive datetimes are a bug class
        dict[str, Any]: JSONB(),
        list[str]: JSONB(),
        # A list of objects - `tasks.inputs`, and every list-of-values column after it. Declared
        # here rather than as an inline `mapped_column(JSONB)` on each model: the annotation map is
        # the ONE place a Python type becomes a Postgres type, and the day it has two, half the
        # columns of one shape will be `JSONB` and half `JSON`, and nobody will know why.
        list[dict[str, Any]]: JSONB(),
    }


class StrEnumColumn[E: StrEnum](TypeDecorator[E]):
    """A vocabulary column: VARCHAR in Postgres, so no migration and no `ALTER TYPE` for a new word,
    and its one `StrEnum` in Python, both ways. A word outside the enum raises, written or read."""

    impl = String
    cache_ok = True

    def __init__(self, enum: type[E], length: int) -> None:
        super().__init__(length)
        self.enum = enum
        self.length = length

    @override
    def process_bind_param(self, value: E | str | None, dialect: Dialect) -> str | None:
        return None if value is None else self.enum(value).value

    @override
    def process_result_value(self, value: str | None, dialect: Dialect) -> E | None:
        return None if value is None else self.enum(value)


def vocabulary(column: str, enum: type[StrEnum]) -> CheckConstraint:
    """The CHECK that holds a status or kind column to its enum's words, named
    `ck_<table>_<column>_vocabulary`. The column stays VARCHAR; a new word ships with a migration
    that replaces this constraint (`test_vocabulary_checks` fails until it does)."""
    words = ", ".join(f"'{member.value}'" for member in enum)
    return CheckConstraint(f"{column} IN ({words})", name=f"{column}_vocabulary")


def uuid_pk() -> Mapped[uuid.UUID]:
    """Client-side uuid4 - the id must exist *before* commit so `task-{id}` can be enqueued after it."""
    return mapped_column(primary_key=True, default=uuid.uuid4)


def created_at() -> Mapped[dt.datetime]:
    return mapped_column(server_default=func.now(), nullable=False)


def utcnow() -> dt.datetime:
    """The one clock. Timezone-aware, always."""
    return dt.datetime.now(dt.UTC)


def rows_affected(result: Result[Any]) -> int:
    """The rows a Core UPDATE or DELETE touched. `AsyncSession.execute` is typed to hand back a
    plain `Result`, but a DML statement's is a `CursorResult`, the one that carries `rowcount`."""
    if not isinstance(result, CursorResult):
        raise TypeError(f"rows_affected wants a DML statement's result, got {type(result).__name__}")
    return result.rowcount or 0


def escape_like(q: str) -> str:
    """`q` as an ILIKE *literal*: user-typed `%`/`_`/`\\` match themselves, never wildcard.
    One definition - this recipe existed in three models and had already
    been mangled twice by tooling. Pair with `.ilike(f"%{escape_like(q)}%", escape="\\\\")`."""
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def keyset_cursor(row: Any, *, ident: str = "id") -> str:
    """The `(created_at, id)` keyset cursor - opaque to clients, meaningful to the index.
    `ident` names the tiebreaker column when a table's identity is not `id`: `users` keys on
    `actor_id` (it IS its actor), and one cursor recipe with a parameter beats a second one
    hand-written on the model - the two drifted the moment they existed."""
    return f"{row.created_at.isoformat()}|{getattr(row, ident)}"


def keyset_before(cls: Any, cursor: str, *, ident: str = "id") -> Any:
    """WHERE clause for the page after `cursor`, newest-first: `(created_at, id) < (at, id)`.
    The tuple compare matches the `(created_at DESC, id DESC)` order every list uses.
    `ident` mirrors `keyset_cursor`'s - the producer and the consumer take the same argument.

    The cursor is shape-validated at the door (`api/deps.Page`), so a hand-edited `?cursor=`
    is a 422 there and never reaches this parse."""
    from sqlalchemy import tuple_

    at_str, _, id_str = cursor.partition("|")
    return tuple_(cls.created_at, getattr(cls, ident)) < (
        dt.datetime.fromisoformat(at_str),
        uuid.UUID(id_str),
    )
