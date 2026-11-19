# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A turn runs on the code that enqueued it (amended for ADR-062).

DBOS dequeues a workflow only on a worker of its stamped application version - or on ANY worker
when the stamp is NULL. The API's enqueues carried no stamp, so a worker of another release (one
that predates the memory fence) could take a Project turn and run it unfenced. Pinned here: the API
stamps the version both processes read, a worker's beat names its own, the API hears a live
disagreement at boot, and the worker's boot sweep moves a never-started ghost onto its own
version (deleted, then enqueued again under its own id with its own inputs) instead of stranding
it behind a cancelled row - a turn, and a chat reply alike.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

import pytest

from prismoid.conf import settings
from prismoid.db.models.beat import STALE_AFTER_S, Beat
from prismoid.jobs import durable
from prismoid.jobs.dispatcher import ClientDispatcher
from prismoid.operations import health
from prismoid.runner import worker


class _Client:
    def __init__(self) -> None:
        self.options: dict[str, Any] = {}

    async def enqueue_async(self, options: dict[str, Any], **_: Any) -> None:
        self.options = dict(options)


def _dispatcher() -> tuple[ClientDispatcher, _Client]:
    """The real dispatcher over the real `durable.Client`, whose engine client is the fake: the
    options asserted are what the engine is handed."""
    client = _Client()
    engine = durable.Client.__new__(durable.Client)
    engine._client = client  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage]
    dispatcher = ClientDispatcher.__new__(ClientDispatcher)
    dispatcher._client = engine  # pyright: ignore[reportPrivateUsage]
    return dispatcher, client


async def test_the_api_stamps_its_version_on_every_enqueue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.worker, "app_version", "sha-1a")
    dispatcher, client = _dispatcher()

    await dispatcher.enqueue("run_task", "task-123-t2", task_id="123", turn=2)

    assert client.options["app_version"] == "sha-1a"
    assert client.options["workflow_id"] == "task-123-t2"


async def test_an_unpinned_api_stamps_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty is the worker's opt-out to DBOS's code hash, which the API cannot know: stamping ""
    would match no worker at all, so the turn stays unstamped - the old behaviour, on purpose."""
    monkeypatch.setattr(settings.worker, "app_version", "")
    dispatcher, client = _dispatcher()

    await dispatcher.enqueue("run_task", "task-123", task_id="123")

    assert "app_version" not in client.options


def _beat(monkeypatch: pytest.MonkeyPatch, age: float | None, info: dict[str, Any]) -> None:
    async def read(_session: object, _name: str) -> tuple[float | None, dict[str, Any]]:
        return age, info

    monkeypatch.setattr(Beat, "read", read)


async def test_the_api_hears_a_live_worker_of_another_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.worker, "app_version", "sha-new")
    _beat(monkeypatch, 12.0, {"version": "sha-old"})

    skew = await health.version_skew(object())  # pyright: ignore[reportArgumentType]

    assert skew is not None and "sha-old" in skew and "sha-new" in skew


@pytest.mark.parametrize(
    ("age", "info"),
    [
        (12.0, {"version": "sha-new"}),  # they agree
        (STALE_AFTER_S + 1.0, {"version": "sha-old"}),  # no LIVE worker to disagree with
        (None, {}),  # no worker ever beat
        (12.0, {"queues": {}}),  # a beat from before the version rode it
    ],
)
async def test_no_skew_without_a_live_disagreement(
    monkeypatch: pytest.MonkeyPatch, age: float | None, info: dict[str, Any]
) -> None:
    monkeypatch.setattr(settings.worker, "app_version", "sha-new")
    _beat(monkeypatch, age, info)

    assert await health.version_skew(object()) is None  # pyright: ignore[reportArgumentType]


def _run(
    key: str, state: durable.RunState, version: str | None, name: str = "run_task", **kwargs: Any
) -> durable.Run:
    return durable.Run(key, name, state, "tasks", version, None, args=(), kwargs=kwargs)


class _Engine:
    """Plays the system DB the boot sweep reads and writes, behind the durable seam."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, rows: list[durable.Run]) -> None:
        self.rows = rows
        self.deleted: list[str] = []
        self.cancelled: list[str] = []
        self.enqueued: list[tuple[str, Any, dict[str, Any]]] = []
        self.asked: list[Collection[str]] = []  # the states each listing asked for
        monkeypatch.setattr(durable, "version", lambda: "sha-new")
        monkeypatch.setattr(durable, "runs_sync", self.runs)
        monkeypatch.setattr(durable, "forget_sync", self.deleted.append)
        monkeypatch.setattr(durable, "cancel_sync", self.cancelled.append)
        monkeypatch.setattr(durable, "start_sync", self.start)

    def runs(self, *, states: Collection[str], limit: int) -> list[durable.Run]:
        self.asked.append(states)
        return self.rows

    def start(self, _lane: Any, key: str, fn: Any, **kwargs: Any) -> None:
        self.enqueued.append((key, fn, kwargs))


def test_the_boot_sweep_frees_a_never_started_ghost_and_cancels_a_started_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _Engine(
        monkeypatch,
        [
            _run("task-1", "queued", "sha-old"),  # the old API's turn, never started
            _run("step-2", "running", "sha-old"),  # started on code that is gone
            _run("task-3-t2", "queued", "sha-new"),  # ours - left alone
            _run("task-4", "queued", None),  # unstamped - any worker takes it
        ],
    )

    assert worker._sweep_version_ghosts() == 2  # pyright: ignore[reportPrivateUsage]

    assert engine.deleted == ["task-1"], "its deterministic id is free for the sweeper's re-enqueue"
    assert engine.cancelled == ["step-2"]


def test_the_boot_sweep_moves_a_never_started_job_onto_this_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deploy deleted the old version's never-started jobs, and only turns had a sweeper to bring
    them back: a line sent to a bot while the worker was down got no reply, ever (the old API's
    `reply_chat` carried the old stamp). The sweep enqueues each again on this version, under its
    own id, with its own inputs; a job this worker does not know stays deleted. An inbox alert
    still waiting out its delay moves the same way (it starts at once): left waiting, it would
    join a lane no worker of its version is left to take."""
    from prismoid.jobs import dispatcher
    from prismoid.jobs.push import push_inbox

    monkeypatch.setitem(dispatcher._JOBS, "reply_chat", ("reply_fn", "chat-lane", "chat"))  # pyright: ignore[reportPrivateUsage]
    reply = {"conversation_id": "c-1", "seq": 7}
    engine = _Engine(
        monkeypatch,
        [
            _run("chat-c-1-7", "queued", "sha-old", name="reply_chat", **reply),
            _run("gone-1", "queued", "sha-old", name="retired_job", **reply),
            _run("push-inbox-1", "delayed", "sha-old", name="push_inbox", item_id="1"),
        ],
    )

    assert worker._sweep_version_ghosts() == 3  # pyright: ignore[reportPrivateUsage]

    assert "delayed" in engine.asked[0]
    assert engine.deleted == ["chat-c-1-7", "gone-1", "push-inbox-1"] and engine.cancelled == []
    assert engine.enqueued == [
        ("chat-c-1-7", "reply_fn", {"conversation_id": "c-1", "seq": 7}),
        ("push-inbox-1", push_inbox, {"item_id": "1"}),
    ]


async def test_a_beat_that_carries_only_its_version_reads_queues_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The version rides every beat, so a beat whose queue vitals failed is no longer empty - it
    must still read "unknown", never "ok"."""
    _beat(monkeypatch, 12.0, {"version": "sha-new"})

    class _Session:
        async def execute(self, _statement: object) -> None:
            return None

        async def scalar(self, _statement: object) -> str:
            return "f58c9003cdd1"

    report = await health.probe(_Session())  # pyright: ignore[reportArgumentType]

    assert report.worker == "ok" and report.queues == "unknown"
