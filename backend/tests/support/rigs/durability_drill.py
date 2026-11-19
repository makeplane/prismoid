# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The scripted step the call-log drills run (ADR-089), and an in-memory call log.

A `World` scripts a five-request step whose first four responses call tools and whose fifth answers (in
text, or by a writer's `submit_document`), counts every provider call it serves and, when told, dies at a
chosen point: before request `n` is sent (after the rows of the requests before it are written) or inside
it, once the provider has answered and before its row is written. The death is the world's own (`Killed` in process, SIGKILL
in a rig), never a hook in product code. `MemoryLog` is the `CallLog` port over a dict, with the
store's semantics: durable on return, a duplicate unit and phase a no-op, payloads JSON round-tripped.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel
from pydantic_core import from_json, to_json

from prismoid.ai.deps import CallEntry, Generation, RunKey
from prismoid.schemas.ids import StepId
from tests.support.paths import BACKEND

REQUESTS = 5  # the uninterrupted step's provider calls
_BACKEND = BACKEND
_RIG = "tests.drills.durability.rig"  # the module each SIGKILLed attempt runs as


class Killed(BaseException):
    """The in-process stand-in for a SIGKILL: nothing in the engine catches it."""


def die_in_process() -> None:
    raise Killed


@dataclass
class World:
    """What the scripted provider served, and where it dies. `ledger` makes the count survive a SIGKILL."""

    calls: list[list[tuple[str, dict[str, Any]]]]  # the tool calls of requests 1..4
    ledger: Path | None = None
    die_before: int | None = None  # die as request n is about to be sent
    die_inside: int | None = None  # die inside request n, after the provider answered
    die: Callable[[], None] = die_in_process
    output_tool: str | None = None  # the document tool a writer answers through (title, content), else text
    served: int = 0
    effects: list[str] = field(default_factory=list[str])

    @property
    def provider_calls(self) -> int:
        """Every request served, across processes when a ledger is kept."""
        if self.ledger is None:
            return self.served
        return len(self.ledger.read_text().splitlines()) if self.ledger.exists() else 0

    def model(self) -> FunctionModel:
        """The scripted provider, streaming and not."""
        return FunctionModel(self._respond, stream_function=self._stream)

    def _parts(self, messages: list[ModelMessage]) -> list[TextPart | ToolCallPart]:
        n = sum(isinstance(m, ModelResponse) for m in messages) + 1
        if self.die_before == n:
            self.die()
        self.served += 1
        if self.ledger is not None:
            with self.ledger.open("a") as ledger:
                ledger.write(f"request {n}\n")
                ledger.flush()
                os.fsync(ledger.fileno())
        if self.die_inside == n:
            self.die()
        if n <= len(self.calls):
            return [ToolCallPart(name, args) for name, args in self.calls[n - 1]]
        done = f"Done after {n} requests."
        if self.output_tool is not None:
            return [ToolCallPart(self.output_tool, {"title": "Memo", "content": done})]
        return [TextPart(done)]

    def _respond(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=self._parts(messages))

    async def _stream(
        self, messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str | DeltaToolCalls]:
        for index, part in enumerate(self._parts(messages)):
            if isinstance(part, TextPart):
                yield part.content
            else:
                yield {index: DeltaToolCall(name=part.tool_name, json_args=to_json(part.args).decode())}


def notes_and_looks() -> list[list[tuple[str, dict[str, Any]]]]:
    """Four tool turns over two tools, the first one parallel: `note` changes the world, `look` reads it."""
    return [
        [("note", {"text": "a"}), ("look", {"query": "x"})],
        [("note", {"text": "b"})],
        [("look", {"query": "y"})],
        [("note", {"text": "c"})],
    ]


@dataclass
class MemoryLog:
    """The `CallLog` port in memory, with the store's semantics."""

    rows: dict[tuple[StepId, int], dict[tuple[str, str], CallEntry]] = field(
        default_factory=dict[tuple[StepId, int], dict[tuple[str, str], CallEntry]]
    )
    versions: dict[tuple[StepId, int], int] = field(default_factory=dict[tuple[StepId, int], int])
    writes: int = 0

    async def latest(self, step: StepId) -> Generation:
        generations = [g for s, g in self.rows if s == step]
        if not generations:
            return Generation(key=None, entries={})
        top = max(generations)
        return Generation(RunKey(step, top, self.versions[(step, top)]), dict(self.rows[(step, top)]))

    async def record(self, key: RunKey, entry: CallEntry) -> None:
        self.writes += 1
        entries = self.rows.setdefault((key.step, key.generation), {})
        self.versions.setdefault((key.step, key.generation), key.log_version)
        if (entry.unit, entry.phase) not in entries:
            payload = from_json(to_json(entry.payload))
            entries[(entry.unit, entry.phase)] = CallEntry(
                entry.unit, entry.phase, payload, entry.fingerprint
            )

    def units(self, step: StepId, generation: int = 0) -> Mapping[tuple[str, str], CallEntry]:
        """One generation's rows."""
        return self.rows.get((step, generation), {})


def rig(mode: str, task: uuid.UUID, step: uuid.UUID, ledger: Path, *flags: str) -> tuple[int, str]:
    """One pass of `tests.drills.durability.rig` in its own process: its exit code and its last line."""
    done = subprocess.run(
        [sys.executable, "-m", _RIG, mode, str(task), str(step), str(ledger), *flags],
        cwd=_BACKEND,
        env={**os.environ, "PYDANTIC_AI_NO_BANNER": "1"},
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    said = next((line for line in reversed(done.stdout.splitlines()) if line.startswith("RESULT ")), "")
    assert said or done.returncode == -9, done.stderr[-3000:]
    return done.returncode, said


def bought(ledger: Path) -> int:
    """The provider calls a ledger holds, across every process that served them."""
    return len(ledger.read_text().splitlines()) if ledger.exists() else 0
