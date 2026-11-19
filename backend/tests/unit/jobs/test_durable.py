# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The durable seam's own translations (ADR-069): what each neutral verb hands the engine, and what
comes back. The one test file that may know DBOS's spelling - when the engine changes, these are
the tests that change with `jobs/durable.py`, and nothing else does."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest

from prismoid.jobs import durable


def _status(workflow_id: str, status: str, **inputs: Any) -> SimpleNamespace:
    return SimpleNamespace(
        workflow_id=workflow_id,
        name="machine_seed",
        status=status,
        queue_name="machines",
        app_version="sha-1",
        created_at=1_700_000_000_000,
        input={"args": (), "kwargs": inputs} if inputs else None,
    )


async def test_a_listing_asks_for_every_live_status_and_reads_each_row_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def listing(**kw: Any) -> list[SimpleNamespace]:
        seen.update(kw)
        return [
            _status("a", "ENQUEUED", child_machine_id="c-1"),
            _status("b", "PENDING"),
            _status("c", "DELAYED"),
        ]

    monkeypatch.setattr(durable.DBOS, "list_workflows_async", listing)
    runs = await durable.runs("machine_seed", load_input=True)

    # DELAYED is live: DBOS 2.26 holds an enqueue with a delay there, and counts it active.
    assert sorted(seen["status"]) == ["DELAYED", "ENQUEUED", "PENDING"]
    assert seen["name"] == "machine_seed" and seen["load_input"] is True and seen["load_output"] is False
    assert [(r.key, r.state) for r in runs] == [("a", "queued"), ("b", "running"), ("c", "delayed")]
    assert runs[0].kwargs == {"child_machine_id": "c-1"} and runs[0].args == ()
    assert runs[1].kwargs is None, "a run listed without its input says so - never an empty dict"
    assert (runs[0].queue, runs[0].version, runs[0].created_at_ms) == ("machines", "sha-1", 1_700_000_000_000)


async def test_a_narrowed_listing_asks_only_for_its_states(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def listing(**kw: Any) -> list[SimpleNamespace]:
        seen.update(kw)
        return []

    monkeypatch.setattr(durable.DBOS, "list_workflows_async", listing)
    assert await durable.runs(states=("queued", "running"), limit=500) == []
    assert seen["status"] == ["ENQUEUED", "PENDING"] and seen["limit"] == 500 and seen["name"] is None


def test_an_effect_runs_once_unless_it_says_how_to_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[dict[str, Any]] = []

    def step(**kw: Any) -> Any:
        asked.append(kw)
        return lambda fn: fn

    def never(err: BaseException) -> bool:
        return False

    monkeypatch.setattr(durable.DBOS, "step", step)
    durable.effect()
    durable.effect(attempts=20, interval_s=3.0, backoff=1.5, retry_if=never)

    assert asked[0] == {}, "one attempt: the product's retries are the ladder, never the engine's"
    assert asked[1] == {
        "retries_allowed": True,
        "max_attempts": 20,
        "interval_seconds": 3.0,
        "backoff_rate": 1.5,
        "should_retry": never,
    }


async def test_a_reset_cleanup_error_its_predicate_refuses_surfaces_as_itself() -> None:
    """`machine_reset`'s effect retries only a persistent frame's driver that did not answer
    (`reset_retryable`). The engine's retry loop, wired as `invoke_step` wires it: an error the
    predicate refuses - every error a cloud cleanup can raise, from the object store - is raised as
    itself after one attempt, exactly as the one-attempt effect raised it; never DBOS's "retries
    exhausted" wrapper. Only `FrameUnreachable` is asked again, and wrapped once the attempts run out."""
    from dbos._error import DBOSMaxStepRetriesExceeded
    from dbos._outcome import Pending

    from prismoid.ai.frames.driver import FrameUnreachable
    from prismoid.jobs.machines import _reset_retries  # pyright: ignore[reportPrivateUsage]

    calls: list[Exception] = []

    async def retried(err: Exception, attempts: int) -> None:
        async def run() -> None:
            calls.append(err)
            raise err

        await Pending._retry(  # pyright: ignore[reportPrivateUsage]
            run,
            attempts,
            lambda i, e: 0.0,
            lambda i, e: DBOSMaxStepRetriesExceeded("_reset", i, e),
            _reset_retries,
        )

    for cloud in (RuntimeError("the object store is down"), OSError("no route"), ValueError("a bug")):
        calls.clear()
        with pytest.raises(type(cloud)) as raised:
            await retried(cloud, 20)
        assert raised.value is cloud and calls == [cloud], "the original, after one attempt"

    away = FrameUnreachable("the device helper did not answer")
    calls.clear()
    with pytest.raises(DBOSMaxStepRetriesExceeded):
        await retried(away, 3)
    assert calls == [away] * 3


def test_a_schedule_reads_five_fields_or_six_with_seconds_first() -> None:
    at = dt.datetime(2026, 10, 29, 12, 0, 10, tzinfo=dt.UTC)
    assert durable.next_fire("*/30 * * * * *", at) == at.replace(second=30)
    assert durable.next_fire("* * * * *", at) == at.replace(minute=1, second=0)


class _EngineClient:
    """Plays `DBOSClient`: records what the seam hands it."""

    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, Any], dict[str, Any]]] = []

    async def enqueue_async(self, options: dict[str, Any], **kwargs: Any) -> None:
        self.calls.append((dict(options), kwargs))


async def test_the_apis_enqueue_hands_a_jobs_own_version_argument_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`test_bot` and `machine_egress` take an argument called `version`: it is the job's, and it
    must never collide with the version the API stamps (it raised TypeError)."""
    from prismoid.conf import settings
    from prismoid.jobs.dispatcher import ClientDispatcher

    monkeypatch.setattr(settings.worker, "app_version", "sha-1a")
    engine = _EngineClient()
    client = durable.Client.__new__(durable.Client)
    client._client = engine  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage]
    dispatcher = ClientDispatcher.__new__(ClientDispatcher)
    dispatcher._client = client  # pyright: ignore[reportPrivateUsage]

    await dispatcher.enqueue("machine_egress", "machine-egress-m-1-7", machine_id="m-1", version=7)
    await dispatcher.enqueue("test_bot", "bottest-1", actor_id="a", name="b", version=3, workspace_id="w")

    (egress, egress_kwargs), (bot, bot_kwargs) = engine.calls
    assert egress == {
        "queue_name": "machines",
        "workflow_name": "machine_egress",
        "workflow_id": "machine-egress-m-1-7",
        "app_version": "sha-1a",
    }
    assert egress_kwargs == {"machine_id": "m-1", "version": 7}
    assert bot["app_version"] == "sha-1a" and bot_kwargs["version"] == 3


async def test_a_start_runs_under_its_key_and_passes_a_jobs_key_argument_through() -> None:
    from dbos._context import get_local_dbos_context

    seen: list[tuple[str | None, tuple[Any, ...], dict[str, Any]]] = []

    class Lane:
        async def enqueue_async(self, fn: Any, *args: Any, **kwargs: Any) -> str:
            ctx = get_local_dbos_context()
            seen.append((ctx.id_assigned_for_next_workflow if ctx else None, args, kwargs))
            return "handle"

    async def job(*args: Any, **kwargs: Any) -> None:
        return None

    lane: Any = Lane()
    assert await durable.start(lane, "step-1", job, "t-1", key="the job's own") == "handle"
    assert seen == [("step-1", ("t-1",), {"key": "the job's own"})]


async def test_a_wait_parks_on_its_topic_for_its_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[dict[str, Any]] = []

    async def recv(**kw: Any) -> str:
        asked.append(kw)
        return "woken"

    monkeypatch.setattr(durable.DBOS, "recv_async", recv)
    assert await durable.wait("gate", timeout_s=86400) == "woken"
    assert asked == [{"topic": "gate", "timeout_seconds": 86400}]


async def test_a_row_whose_input_cannot_be_read_is_unknown_not_a_failed_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DBOS hands back the raw string for an input it cannot deserialize - an old release's row,
    exactly what the boot ghost sweep exists to read."""
    unreadable = _status("old", "ENQUEUED")
    unreadable.input = "gASV...an old release's pickle"

    async def listing(**kw: Any) -> list[SimpleNamespace]:
        return [unreadable, _status("new", "ENQUEUED", conversation_id="c-1")]

    monkeypatch.setattr(durable.DBOS, "list_workflows_async", listing)
    old, new = await durable.runs(load_input=True)
    assert (old.args, old.kwargs) == (None, None)
    assert new.kwargs == {"conversation_id": "c-1"}


@pytest.mark.parametrize("environment", ["local", "dev", "prod"])
def test_no_environment_runs_the_admin_server(monkeypatch: pytest.MonkeyPatch, environment: str) -> None:
    """The engine's admin server binds 0.0.0.0:3001 with no auth: never on a Mac, never in a cluster."""
    from prismoid.conf import settings

    seen: list[Any] = []
    monkeypatch.setattr(durable, "DBOS", lambda config: seen.append(config))
    monkeypatch.setattr(settings, "environment", environment)
    durable.init(name="prismoid", database_url="postgresql://x@/prismoid", executor_id=None, version="v1")
    assert dict(seen[-1]) == {
        "name": "prismoid",
        "database_url": "postgresql://x@/prismoid",
        "executor_id": None,
        "application_version": "v1",
        "enable_otlp": True,
        "run_admin_server": False,
        "max_executor_threads": 64,
    }


async def test_a_start_from_inside_an_effect_starts_its_job_as_a_root() -> None:
    """DBOS asserts when a workflow starts inside a step, so an effect's enqueue (the early boot from
    `mark_dequeued`) once failed every time. It starts in a context with no job around it, under its
    key, and the effect's own context is left as it was."""
    from dbos._context import DBOSContext, DBOSContextEnsure, get_local_dbos_context

    seen: list[tuple[str, bool]] = []

    class Lane:
        async def enqueue_async(self, fn: Any, *args: Any, **kwargs: Any) -> str:
            ctx = DBOSContext.create_start_workflow_child(get_local_dbos_context())  # asserts in a step
            seen.append((ctx.workflow_id or ctx.id_assigned_for_next_workflow, ctx.is_within_workflow()))
            return "handle"

    async def job(*args: Any, **kwargs: Any) -> None:
        return None

    with DBOSContextEnsure() as effect:
        effect.workflow_id, effect.function_id, effect.curr_step_function_id = "task-1", 2, 2  # in a step
        lane: Any = Lane()
        assert await durable.start(lane, "machine-boot-1", job, task_id="1") == "handle"
        assert seen == [("machine-boot-1", False)], "a root, keyed by its own id"
        assert get_local_dbos_context() is effect and effect.function_id == 2, "the effect is untouched"
        effect.workflow_id, effect.function_id, effect.curr_step_function_id = "", -1, -1


async def test_a_later_start_holds_its_job_back_and_a_plain_one_does_not() -> None:
    """`start_later` hands the engine a due time `delay_s` from now, under the job's key; `start`,
    and a delay of 0, hand it none, so the job joins its lane at once."""
    import time

    from dbos._context import get_local_dbos_context

    seen: list[tuple[str | None, int | None]] = []

    class Lane:
        async def enqueue_async(self, fn: Any, *args: Any, **kwargs: Any) -> str:
            ctx = get_local_dbos_context()
            assert ctx is not None
            seen.append((ctx.id_assigned_for_next_workflow, ctx.delay_until_epoch_ms))
            return "handle"

    async def job(**kwargs: Any) -> None:
        return None

    lane: Any = Lane()
    before_ms = int(time.time() * 1000)
    await durable.start_later(lane, "push-inbox-1", 5.0, job, item_id="1")
    await durable.start_later(lane, "push-inbox-2", 0.0, job, item_id="2")
    await durable.start(lane, "push-read-3", job, inbox_ids="3")

    (late_key, due_ms), now_two, now_three = seen
    assert late_key == "push-inbox-1" and due_ms is not None
    assert before_ms + 5_000 <= due_ms <= int(time.time() * 1000) + 5_000
    assert now_two == ("push-inbox-2", None) and now_three == ("push-read-3", None)


async def test_both_dispatchers_pass_on_the_delay_they_are_given(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dispatchers know no job's delay: the one a caller asks for reaches the engine from the
    API and the worker alike, never as one of the job's own arguments, and a job asked to wait for
    nothing starts at once."""
    from prismoid.conf import settings
    from prismoid.jobs.dispatcher import ClientDispatcher, WorkerDispatcher

    monkeypatch.setattr(settings.worker, "app_version", "")
    engine = _EngineClient()
    monkeypatch.setattr(durable, "DBOSClient", lambda **_: engine)  # the API's engine client
    api = ClientDispatcher()
    started: list[tuple[str, float, dict[str, Any]]] = []

    async def start_later(_lane: Any, key: str, delay_s: float, _fn: Any, **kwargs: Any) -> None:
        started.append((key, delay_s, kwargs))

    monkeypatch.setattr(durable, "start_later", start_later)

    await api.enqueue("push_inbox", "push-inbox-1", delay_s=4.0, item_id="1")
    await api.enqueue("push_read", "push-read-1", actor_id="a", workspace_id="w", inbox_ids="1")
    await WorkerDispatcher().enqueue("push_inbox", "push-inbox-2", delay_s=4.0, item_id="2")
    await WorkerDispatcher().enqueue("run_task", "task-3", task_id="3")

    (late, late_kwargs), (now, _) = engine.calls
    assert late["delay_seconds"] == 4.0 and late_kwargs == {"item_id": "1"}
    assert "delay_seconds" not in now
    assert started == [("push-inbox-2", 4.0, {"item_id": "2"}), ("task-3", 0.0, {"task_id": "3"})]
