# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""One step attempt in its own process, for the call-log drills (ADR-089): the real `execute_step`,
or the real `run_step` workflow under DBOS, with only the model scripted (`tests/support/rigs/durability_drill.py`).
The scripted provider SIGKILLs this process at the point the drill names; nothing in product code knows.
Every read of a note is an effect, a line in `effects.log` beside the ledger; `--die-in-read N` kills
the process inside the read of `notes-N.md`, after its effect or, with `--before-effect`, before it.

    python -m tests.drills.durability.rig step TASK STEP LEDGER [--die-before N | --die-inside N | --die-in-read N [--before-effect]]
        [--ask] [--handed-back N] [--log-version N] [--die-on-resume] [--compact [--die-at-handoff]]
    python -m tests.drills.durability.rig dbos TASK STEP LEDGER [--start] [--die-before N]

`--compact`: the writer reads eight long notes under a bound small enough that the run hands off, its
handoff written by the summarizer beside it (each summary a line in `summaries.log`), both models metered
into the real ledger; with `--die-at-handoff` the process dies as the first request carrying a handoff is
sent, the summary already logged.

`step` runs the ladder directly (`--handed-back N`: as a workflow body that has taken N hand-backs;
`--ask`: the writer asks a question after its second read; `--log-version N`: as code of that call-log
version would; `--die-on-resume`: SIGKILLed as the parked run resumes, its wake committed and its answer not
yet logged). `dbos` boots the durable runtime (which recovers whatever this
executor had in flight), starts the step's `run_step` job when `--start` is given, and waits for the
step to settle. Prints one `RESULT <state>` line.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import os
import signal
import time
import uuid
from collections.abc import AsyncIterator
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import psycopg
import pydantic_ai.models.function as function_model
import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import (
    AgentInfo,
    DeltaToolCall,
    DeltaToolCalls,
    FunctionModel,
    FunctionStreamedResponse,
)
from pydantic_ai.usage import RequestUsage
from pydantic_core import to_json

import prismoid.ai.agents.base as agents_base
from prismoid.ai.capabilities.compaction import HANDOFF, Compaction, estimated
from prismoid.ai.capabilities.durability import CallLogScope
from prismoid.ai.frames.paper import PaperFrame
from prismoid.ai.gateway.classes import ModelSpec
from prismoid.ai.gateway.meter import MeterContext, MeteredModel
from prismoid.conf import settings
from prismoid.operations.execution import harvest
from prismoid.operations.execution import run as step_run
from prismoid.schemas.model_class import Family, ModelClass
from prismoid.schemas.outputs import SUBMIT_DOCUMENT
from tests.support.rigs.durability_drill import World

# A writer's step on paper: four reads, then the memo it submits.
WRITER_CALLS: list[list[tuple[str, dict[str, Any]]]] = [
    [("read_file", {"path": f"/work/notes-{n}.md"})] for n in range(1, 5)
]
# The same step with one question between its second and third reads: it parks there.
ASKING_CALLS: list[list[tuple[str, dict[str, Any]]]] = [
    *WRITER_CALLS[:2],
    [("ask_user", {"questions": [{"question": "Formal or casual?", "options": []}]})],
    *WRITER_CALLS[2:],
]


# The compaction drill: eight reads of a ~3k-token note under a bound that hands off from about the
# fourth request (its trigger is half the window).
COMPACT_READS = 8
COMPACT_WINDOW = 16_000
NOTE = "".join(f"finding {n}: the figure holds and its source is cited.\n" for n in range(240))
_REPORTED: ContextVar[int] = ContextVar("reported", default=0)  # the request a scripted stream answers
DRILL_SPEC = ModelSpec("drill", "scripted", Family("drill"), "openai", tuple(ModelClass))


def sigkill() -> None:
    os.kill(os.getpid(), signal.SIGKILL)


def _scripted(args: argparse.Namespace) -> None:
    world = World(
        ASKING_CALLS if args.ask else WRITER_CALLS,
        ledger=Path(args.ledger),
        die_before=args.die_before,
        die_inside=args.die_inside,
        output_tool=SUBMIT_DOCUMENT,  # the writer finishes with its typed document
    )
    world.die = sigkill
    patch = pytest.MonkeyPatch()
    patch.setattr(agents_base, "resolve", lambda *_, **__: world.model())
    patch.setattr(harvest, "extract_previews", _no_previews)  # renders need a real frame
    patch.setattr(PaperFrame, "read_file", _dying_read(args))
    if args.log_version is not None:
        patch.setattr(CallLogScope, "open", _opened_as(args.log_version))
    if args.die_on_resume:
        patch.setattr(step_run, "_resumed", lambda *_, **__: sigkill())
    if args.compact:
        _compacting(args, patch)


def _compacting(args: argparse.Namespace, patch: pytest.MonkeyPatch) -> None:
    """The compaction drill's world: long notes, a small bound, a scripted writer and summarizer."""
    ledger, summaries = Path(args.ledger), Path(args.ledger).with_name("summaries.log")
    real_for_seat = Compaction.for_seat.__func__

    def for_seat(cls: type[Compaction], *a: Any, **k: Any) -> Compaction:
        return dataclasses.replace(
            real_for_seat(cls, *a, **k), cleared_rows=frozenset(), window=COMPACT_WINDOW
        )

    async def read(frame: PaperFrame, path: str) -> str:
        return f"{path}\n{NOTE}"

    async def write(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        _REPORTED.set(estimated(messages))
        if args.die_at_handoff and any(HANDOFF in str(getattr(p, "content", "")) for p in messages[0].parts):
            sigkill()
        _fsynced(ledger, "request")
        n = _next_read(messages)
        if n > COMPACT_READS:
            done = to_json({"title": "Memo", "content": f"Done after {COMPACT_READS} reads."}).decode()
            yield {0: DeltaToolCall(name=SUBMIT_DOCUMENT, json_args=done)}
        else:
            yield {0: DeltaToolCall(name="read_file", json_args=f'{{"path": "/work/notes-{n}.md"}}')}

    def summarize(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        _fsynced(summaries, "summary")
        return ModelResponse(parts=[TextPart("Asked: a memo from the notes. Findings: every figure holds.")])

    def resolve(
        model_class: ModelClass, *_: Any, emit_usage: Any = None, context: Any = None, **__: Any
    ) -> Any:
        scripted = (
            FunctionModel(summarize)
            if model_class is ModelClass.DECIDER
            else FunctionModel(stream_function=write)
        )
        return MeteredModel(scripted, DRILL_SPEC, emit_usage, context or MeterContext())

    patch.setattr(Compaction, "for_seat", classmethod(for_seat))
    patch.setattr(function_model, "FunctionStreamedResponse", _ReportedStream)
    patch.setattr(PaperFrame, "read_file", read)
    patch.setattr(agents_base, "resolve", resolve)


class _ReportedStream(FunctionStreamedResponse):
    """A scripted stream reporting its request's input as a provider does: a `FunctionModel` stream
    reports a flat 50 tokens, which would hide the history's size from the bound."""

    def __post_init__(self) -> None:
        self._usage = RequestUsage(input_tokens=_REPORTED.get())


def _next_read(messages: list[ModelMessage]) -> int:
    """The next note to read, from the newest call: a handoff drops the old ones."""
    for message in reversed(messages):
        for part in message.parts:
            if isinstance(part, ToolCallPart):
                return int(str(part.args_as_dict()["path"]).split("-")[1].split(".")[0]) + 1
    return 1


def _fsynced(path: Path, line: str) -> None:
    with path.open("a") as log:
        log.write(f"{line}\n")
        log.flush()
        os.fsync(log.fileno())


def _dying_read(args: argparse.Namespace) -> Any:
    """The paper frame's read, logging each note read as an effect and dying in the one the drill names."""
    real = PaperFrame.read_file
    target = f"/work/notes-{args.die_in_read}.md" if args.die_in_read is not None else None
    effects = Path(args.ledger).with_name("effects.log")

    async def read(frame: PaperFrame, path: str) -> str:
        if path == target and args.before_effect:
            sigkill()
        if "/notes-" not in path:
            return await real(frame, path)
        with effects.open("a") as log:
            log.write(f"{path}\n")
            log.flush()
            os.fsync(log.fileno())
        if path == target:
            sigkill()
        return await real(frame, path)

    return read


def _opened_as(version: int) -> Any:
    """`CallLogScope.open` as code of another `LOG_VERSION` calls it."""
    real = CallLogScope.open.__func__

    async def open_(cls: type[CallLogScope], log: Any, step: Any, *, fresh: bool, **_: Any) -> CallLogScope:
        return await real(cls, log, step, fresh=fresh, version=version)

    return classmethod(open_)


async def _no_previews(frame: Any, manifest: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    return {}, {}


async def _step(task_id: uuid.UUID, step_id: uuid.UUID, handed_back: int | None) -> str:
    from prismoid.db.models.task import Task
    from prismoid.db.session import row_scope
    from prismoid.operations import execution

    async with row_scope(Task, task_id) as session:
        return await execution.execute_step(session, task_id, step_id, handed_back=handed_back)


def _state(step_id: uuid.UUID) -> str:
    with psycopg.connect(settings.worker.database_url) as db:
        row = db.execute("select state from plan_steps where id = %s", (step_id,)).fetchone()
    return str(row[0]) if row else "missing"


def _dbos(task_id: uuid.UUID, step_id: uuid.UUID, *, start: bool) -> str:
    from prismoid.jobs import all as jobs
    from prismoid.jobs import durable, effects
    from prismoid.jobs.queues import step_queue

    if not jobs.JOBS:
        raise SystemExit("no jobs registered")
    durable.init(
        name="prismoid", database_url=settings.worker.database_url, executor_id=None, version="drill"
    )
    durable.launch()
    try:
        if start:
            durable.start_sync(
                step_queue, f"step-{step_id}", effects.execute_step, str(task_id), str(step_id)
            )
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and _state(step_id) not in ("done", "escalated"):
            time.sleep(0.2)
        return _state(step_id)
    finally:
        durable.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("step", "dbos"))
    parser.add_argument("task")
    parser.add_argument("step")
    parser.add_argument("ledger")
    parser.add_argument("--die-before", type=int)
    parser.add_argument("--die-inside", type=int)
    parser.add_argument("--die-in-read", type=int)
    parser.add_argument("--before-effect", action="store_true")
    parser.add_argument("--start", action="store_true")
    parser.add_argument("--ask", action="store_true")
    parser.add_argument("--handed-back", type=int)
    parser.add_argument("--log-version", type=int)
    parser.add_argument("--die-on-resume", action="store_true")
    parser.add_argument("--compact", action="store_true")
    parser.add_argument("--die-at-handoff", action="store_true")
    args = parser.parse_args()
    _scripted(args)
    task_id, step_id = uuid.UUID(args.task), uuid.UUID(args.step)
    if args.mode == "step":
        said = asyncio.run(_step(task_id, step_id, args.handed_back)).replace("\x00", "")
    else:
        said = _dbos(task_id, step_id, start=args.start)
    print(f"RESULT {said}", flush=True)


if __name__ == "__main__":
    main()
