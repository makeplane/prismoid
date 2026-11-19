# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The source store: the text of every page a task's `web.fetch` read, kept for the receipt.

`keeper` seals each page's text with `operations/cipher` (the call log's posture: a page fetched for
a person rides the same encryption as the run that read it) under `sources/{task_id}/{sha256}`, and
the sha rides that call's `tool.called` row as its `ref`, off the label a person reads. No table: `task_events`
outlives the call log, which goes when the task ends, so the verifier finds the pages afterwards
(`pages`) and a follow-up reads where the thread's words came from (`ledger`, mounted as
`sources.jsonl`, each page with its source id). A fork copies the parent's (`inherit`). The pages go
with the task (`forget`, from `tasks.delete_task`).

Beside them, sealed the same way, `ids.json` holds every source id the task gave (`S1` to its URL as
vetted and the sha its text was kept under): `cite` gives a page its id the first time any step of the task reads it, under the task's
sources lock, so a writer cites the page a researcher read a step earlier by the same id, a page two
steps read has one, and a replayed read keeps the id its recorded return carries; `recall` reads the
page an id names back, so that writer checks a quote on it with no second fetch.
"""

from __future__ import annotations

import io
import uuid
from dataclasses import dataclass
from typing import Final, TypedDict

import logfire
import orjson
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid import storage
from prismoid.ai.tools.fetch import TOOL as FETCH
from prismoid.ai.tools.labels import fetch_label, page_read
from prismoid.ai.tools.research import KeepPage
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope
from prismoid.operations import cipher

# A stored page's name in a verify frame. `ai/verification/suites/quotes.py` holds the same prefix:
# a fleet chair quotes the answers it was given, never a page.
PAGE_PREFIX: Final = "page-"
LEDGER: Final = "sources.jsonl"  # mounted beside a follow-up's earlier deliverables


@dataclass(frozen=True, order=True)
class StoredPage:
    """A page a task read and kept: host+path as its row spells it, and the sha of its text."""

    page: str
    sha256: str


class _Given(TypedDict):
    page: str  # the URL as vetted
    sha: str | None  # the text kept when the id was given, None when it was not kept


def _key(task_id: uuid.UUID, sha256: str) -> str:
    return f"sources/{task_id}/{sha256}"


def _ids_key(task_id: uuid.UUID) -> str:
    return f"sources/{task_id}/ids.json"


async def _given(task_id: uuid.UUID) -> dict[str, _Given]:
    """`ids.json`: every source id the task gave, to its page and kept sha, in the order given."""
    sealed = io.BytesIO()
    try:
        await storage.aget_file(_ids_key(task_id), sealed)
    except storage.Missing:
        return {}
    ids: dict[str, _Given] = orjson.loads(cipher.decrypt_bytes(sealed.getvalue()))
    return ids


async def source_ids(task_id: uuid.UUID) -> dict[str, str]:
    """Every source id the task gave, to its page's URL, in the order given; empty when none was."""
    return {sid: given["page"] for sid, given in (await _given(task_id)).items()}


@dataclass(frozen=True)
class _Keeper:
    """The `keep_page` port for one task."""

    task_id: uuid.UUID

    async def __call__(self, text: str) -> str:
        """Seal the text, store it, answer its sha."""
        data = text.encode()
        sha = storage.sha256(data)
        await storage.aput_file(_key(self.task_id, sha), io.BytesIO(cipher.encrypt_bytes(data)))
        return sha

    async def read(self, sha: str) -> str:
        """The text kept under `sha` (a replayed run's shelf reads its pages back)."""
        sealed = io.BytesIO()
        await storage.aget_file(_key(self.task_id, sha), sealed)
        return cipher.decrypt_bytes(sealed.getvalue()).decode()

    async def cite(self, page: str, sha: str | None) -> str:
        """The page's source id for the whole task: the one it was given, else the next (kept with `sha`),
        under the task's lock."""
        async with row_scope(Task, self.task_id) as session:
            await Task.lock_sources(session, self.task_id)
            ids = await _given(self.task_id)
            given = next((sid for sid, was in ids.items() if was["page"] == page), None)
            if given is None:
                given = f"S{len(ids) + 1}"
                sealed = cipher.encrypt_bytes(orjson.dumps({**ids, given: _Given(page=page, sha=sha)}))
                await storage.aput_file(_ids_key(self.task_id), io.BytesIO(sealed))
            await session.commit()  # the lock goes with the transaction, after the write
        return given

    async def recall(self, source: str) -> str:
        """The text kept with `source` when the task gave it; `LookupError` when none was kept."""
        given = (await _given(self.task_id)).get(source)
        if given is None or given["sha"] is None:
            raise LookupError(source)
        return await self.read(given["sha"])


def keeper(task_id: uuid.UUID) -> KeepPage:
    """The `keep_page` port for one task: seal and store a page's text, and read it back by its sha."""
    return _Keeper(task_id)


async def stored(session: AsyncSession, task_id: uuid.UUID) -> list[StoredPage]:
    """Every page the task kept, from any step and any turn, in page order."""
    return await _stored(session, task_id, await _given(task_id))


async def _stored(session: AsyncSession, task_id: uuid.UUID, given: dict[str, _Given]) -> list[StoredPage]:
    """The pages the task's own reads kept, and those a fork inherited with their ids (`inherit`)."""
    calls = await TaskEvent.ok_tool_refs(session, task_id=task_id, tool=FETCH)
    read = {StoredPage(page, sha) for summary, sha in calls if (page := page_read(FETCH, summary))}
    return sorted(
        read | {StoredPage(fetch_label(was["page"]), was["sha"]) for was in given.values() if was["sha"]}
    )


async def pages(session: AsyncSession, task_id: uuid.UUID) -> dict[str, bytes]:
    """The kept pages' text by their verify-frame name, for the suites to check quotes against."""
    out: dict[str, bytes] = {}
    for kept in await stored(session, task_id):
        name = f"{PAGE_PREFIX}{kept.sha256[:16]}.md"
        if name not in out:
            sealed = io.BytesIO()
            await storage.aget_file(_key(task_id, kept.sha256), sealed)
            out[name] = cipher.decrypt_bytes(sealed.getvalue())
    return out


async def ledger(session: AsyncSession, task_id: uuid.UUID) -> bytes:
    """`sources.jsonl`: one line per kept page, `{"id", "page", "sha256"}` (`id` the source id a revision
    cites it by, when the task gave one), empty when none was kept."""
    given = await _given(task_id)
    ids = {was["sha"]: sid for sid, was in given.items() if was["sha"]}
    return b"".join(
        orjson.dumps(
            {**({"id": ids[p.sha256]} if p.sha256 in ids else {}), "page": p.page, "sha256": p.sha256}
        )
        + b"\n"
        for p in await _stored(session, task_id, given)
    )


async def inherit(parent_id: uuid.UUID, child_id: uuid.UUID) -> None:
    """A fork's sources: the parent's ids and the pages kept with them, copied under the child's prefix as
    they stand at the fork, so a revision cites the parent's pages by the same ids and its quotes check."""
    given = await _given(parent_id)
    for sha in {was["sha"] for was in given.values() if was["sha"]}:
        await storage.acopy_key(_key(parent_id, sha), _key(child_id, sha))
    if given:
        await storage.acopy_key(_ids_key(parent_id), _ids_key(child_id))


async def forget(task_id: uuid.UUID) -> None:
    """Delete the task's kept pages; a store error leaks blobs, never fails the delete (fail-soft)."""
    try:
        await storage.adelete_prefix(f"sources/{task_id}/")
    except Exception:
        logfire.warn("kept pages of task {task} left after delete (store error)", task=str(task_id))
