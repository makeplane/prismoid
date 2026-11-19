# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The call log's store (`ai/deps.CallLog`): a step attempt's logged units, encrypted at rest.

Each `record` is one autocommit insert on the write pool (`db.engine.idempotent_write`: no
session, no BEGIN/COMMIT, no pre-ping), durable when it returns, the first row of a unit's phase
standing; the engine calls it concurrently while the step's session sits committed. The row is a
tenant child keyed by `step_id` with no workspace column, so the insert needs no binding; reads
bind the workspace. A payload already written in the same generation is stored as `payload_ref`
(its sha256), so a connector inventory read at every model step costs one full row; plaintext over
64 KB goes encrypted to the blob store under `call_log/{step_id}/{sha}`, keyed per step so `forget`
deletes a prefix and no other task's row. A task's rows go at each terminal transition (`forget`), inside that transition's transaction;
their blobs go only once that transaction commits, off the commit path and fail-soft (an orphaned
sealed blob is a storage cost, a blob gone under rows a rollback kept is a replay that crashes).
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import uuid
from collections.abc import Iterable
from itertools import groupby

import logfire
from pydantic_core import from_json, to_json
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, SessionTransaction

from prismoid import storage
from prismoid.ai.deps import CallEntry, Generation, RunKey
from prismoid.db.models.call_log import CallLogEntry
from prismoid.db.session import session_scope
from prismoid.operations import cipher
from prismoid.schemas.ids import StepId

BLOB_OVER = 64 * 1024
_SWEEP = "call_log.sweep"  # the session's `info` key: blob prefixes its open transaction forgot
_sweeping: set[asyncio.Task[None]] = set()  # held strongly: a bare task can be collected mid-flight


class PgCallLog:
    """The Postgres call log for one workspace's step attempts."""

    def __init__(self, workspace_id: uuid.UUID) -> None:
        self._workspace = workspace_id
        self._written: dict[tuple[uuid.UUID, int], set[str]] = {}

    async def latest(self, step: StepId) -> Generation:
        """The step's highest generation and every one below it, decrypted, their refs resolved; seeds
        the dedupe set."""
        async with session_scope(self._workspace) as session:
            rows = await CallLogEntry.latest(session, step)
            lower = await CallLogEntry.below(session, step, rows[0].generation) if rows else []
            session.expunge_all()  # the scope's closing rollback would expire them
        if not rows:
            return Generation(key=None, entries={})
        entries, shas = await _decoded(rows)
        earlier = [(await _decoded(g))[0] for _, g in groupby(lower, key=lambda row: row.generation)]
        self._written[(step, rows[0].generation)] = shas
        key = RunKey(step=step, generation=rows[0].generation, log_version=rows[0].log_version)
        return Generation(key=key, entries=entries, earlier=earlier)

    async def record(self, key: RunKey, entry: CallEntry) -> None:
        """Write one row (a duplicate unit and phase is a no-op), durable on return."""
        plain = to_json(entry.payload)
        sha = hashlib.sha256(plain).hexdigest()
        written = self._written.setdefault((key.step, key.generation), set())
        stored: dict[str, object] = {}
        if sha in written:
            stored["payload_ref"] = sha
        elif len(plain) > BLOB_OVER:
            stored["blob_ref"] = blob = f"call_log/{key.step}/{sha}"
            await storage.aput_file(blob, io.BytesIO(cipher.encrypt_bytes(plain)))
        else:
            stored["payload"] = cipher.encrypt_bytes(plain)
        added = await CallLogEntry.add(
            step_id=key.step,
            generation=key.generation,
            unit=entry.unit,
            phase=entry.phase,
            log_version=key.log_version,
            fingerprint=entry.fingerprint,
            **stored,
        )
        if added:
            written.add(sha)


async def _decoded(rows: Iterable[CallLogEntry]) -> tuple[dict[tuple[str, str], CallEntry], set[str]]:
    """One generation's rows as entries, each ref resolved within it, and the shas of its payloads."""
    rows = list(rows)
    by_sha: dict[str, bytes] = {}
    plain: dict[tuple[str, str], bytes] = {}
    for row in rows:
        if row.payload_ref is None:
            plain[(row.unit, row.phase)] = data = await _opened(row)
            by_sha[hashlib.sha256(data).hexdigest()] = data
    entries: dict[tuple[str, str], CallEntry] = {
        (row.unit, row.phase): CallEntry(
            unit=row.unit,
            phase=row.phase,
            payload=from_json(by_sha[row.payload_ref] if row.payload_ref else plain[(row.unit, row.phase)]),
            fingerprint=row.fingerprint,
        )
        for row in rows
    }
    return entries, set(by_sha)


async def _opened(row: CallLogEntry) -> bytes:
    """A full row's plaintext, from the row or its blob."""
    if row.blob_ref is None:
        return cipher.decrypt_bytes(row.payload or b"")
    sealed = io.BytesIO()
    await storage.aget_file(row.blob_ref, sealed)
    return cipher.decrypt_bytes(sealed.getvalue())


async def forget(session: AsyncSession, task_id: uuid.UUID) -> None:
    """Delete the task's rows in the caller's transaction (no commit); their blobs go after it commits."""
    steps = await CallLogEntry.forget(session, task_id)
    sync = session.sync_session
    sync.info.setdefault(_SWEEP, set()).update(f"call_log/{step}/" for step in steps)
    if not event.contains(sync, "after_commit", _sweep_committed):
        event.listen(sync, "after_commit", _sweep_committed)
        event.listen(sync, "after_transaction_end", _drop_uncommitted)


async def flush_sweeps() -> None:
    """Await every blob sweep in flight (the tests' determinism seam)."""
    while _sweeping:
        await asyncio.gather(*tuple(_sweeping), return_exceptions=True)


def _sweep_committed(session: Session) -> None:
    """On the outermost commit, delete the forgotten prefixes in the background."""
    if session.in_nested_transaction():
        return
    for prefix in session.info.pop(_SWEEP, set()):
        task = asyncio.get_running_loop().create_task(_sweep(prefix))
        _sweeping.add(task)
        task.add_done_callback(_sweeping.discard)


def _drop_uncommitted(session: Session, transaction: SessionTransaction) -> None:
    """When the outermost transaction ends without a commit, its rows stand, so their blobs do too."""
    if transaction.parent is None:
        session.info.pop(_SWEEP, None)


async def _sweep(prefix: str) -> None:
    """Delete one step's blobs; a store error leaves them (fail-soft)."""
    try:
        await storage.adelete_prefix(prefix)
    except Exception:
        logfire.warn("call log blobs under {prefix} left after forget (store error)", prefix=prefix)
