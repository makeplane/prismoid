# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Online DDL - the shapes a revision uses so that a LIVE database keeps serving while it runs.

`alembic upgrade head` runs the chain in one transaction (env.py), so every lock a revision takes
is held until the chain's next commit. Three rules keep that window to metadata changes:

1. **Every lock at once, children first, with a timeout.** `lock_tables` takes all the locks a
   window needs before any DDL, children before the tables they reference: the order a live writer
   takes them in (its own table, then a foreign-key check's row lock on the parent), so the two
   never wait on each other in a cycle. Each wait is capped at half the server's
   `deadlock_timeout`: should a multi-statement transaction close a cycle anyway, the migration's
   wait ends first (a lock timeout, never a deadlock), and the attempt is retried from a savepoint
   with nothing held in between. A lock that never comes free (a transaction open for minutes, or
   an autovacuum, which yields only to a waiter that outlasts `deadlock_timeout`) fails the
   upgrade loudly instead of queueing every request behind it.
2. **Foreign keys NOT VALID, validated outside the window.** A key added `NOT VALID` binds every
   new row at once and scans nothing; `VALIDATE CONSTRAINT` checks the old rows later under SHARE
   UPDATE EXCLUSIVE, which readers and writers pass straight through.
3. **Indexes on large tables CONCURRENTLY** (`create_index_concurrently`).

(2) and (3) need autocommit, so they live in a revision of their own whose every statement is
idempotent: alembic writes the version row only after `upgrade()` returns, so a revision whose
transactional half had committed before a crash could never be re-run.

Revisions that change a live table import this module (`vocabulary_checks` is the first); it only
ever grows - a revision's meaning must not change.
"""

from __future__ import annotations

import time
from graphlib import TopologicalSorter

import sqlalchemy as sa
from alembic import op

ACCESS_EXCLUSIVE = "ACCESS EXCLUSIVE"
SHARE_ROW_EXCLUSIVE = "SHARE ROW EXCLUSIVE"  # enough to add a foreign key that references the table
LOCK_ATTEMPTS = 30
LOCK_PAUSE_S = 1.0
_LOCK_NOT_AVAILABLE = "55P03"


def lock_tables(modes: dict[str, str]) -> None:
    """Lock each table in its mode, children first, retrying a timed-out attempt from a savepoint
    (nothing stays held between attempts). Call it first in `upgrade()`, naming every existing
    table the revision touches; the locks hold until the chain's next commit."""
    bind = op.get_bind()
    deadlock_ms = bind.scalar(sa.text("SELECT setting::int FROM pg_settings WHERE name = 'deadlock_timeout'"))
    bind.execute(sa.text(f"SET LOCAL lock_timeout = '{max(deadlock_ms // 2, 10)}ms'"))
    order = _child_first(bind, tuple(modes))
    for attempt in range(1, LOCK_ATTEMPTS + 1):
        try:
            with bind.begin_nested():
                for table in order:
                    bind.execute(sa.text(f"LOCK TABLE {table} IN {modes[table]} MODE"))
            return
        except sa.exc.DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) != _LOCK_NOT_AVAILABLE:
                raise
            if attempt == LOCK_ATTEMPTS:
                raise RuntimeError(
                    f"could not lock {', '.join(order)} in {LOCK_ATTEMPTS} attempts: a long "
                    "transaction or an autovacuum holds one of them (pg_stat_activity names it) - "
                    "rerun once it ends"
                ) from exc
            time.sleep(LOCK_PAUSE_S)


def _child_first(bind: sa.Connection, tables: tuple[str, ...]) -> list[str]:
    """`tables` ordered so each comes before every table it references (the catalog's foreign
    keys among them), else in the order given."""
    edges = bind.execute(
        sa.text(
            "SELECT conrelid::regclass::text, confrelid::regclass::text FROM pg_constraint "
            "WHERE contype = 'f' AND conrelid <> confrelid "
            "AND conrelid::regclass::text = ANY(:t) AND confrelid::regclass::text = ANY(:t)"
        ),
        {"t": list(tables)},
    ).all()
    graph: TopologicalSorter[str] = TopologicalSorter(dict.fromkeys(tables, ()))
    for child, parent in edges:
        graph.add(parent, child)
    return list(graph.static_order())


def create_index_concurrently(name: str, table: str, columns: str) -> None:
    """`CREATE INDEX CONCURRENTLY`, re-runnable: a valid index stands, and an INVALID one (a build
    that died half way) is dropped and rebuilt. Inside `autocommit_block()` only."""
    valid = op.get_bind().scalar(
        sa.text(
            "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = :n"
        ),
        {"n": name},
    )
    if valid:
        return
    if valid is False:
        op.execute(f"DROP INDEX CONCURRENTLY {name}")
    op.execute(f"CREATE INDEX CONCURRENTLY {name} ON {table} ({columns})")
