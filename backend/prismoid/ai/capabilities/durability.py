# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The call log as a run's durable unit: every model request, tool call and logged read of a step
attempt is a row, so a worker that dies mid-step resumes where it stopped and buys nothing twice.

`PrismoidDurability` is pydantic-ai's durable-backend builder with Prismoid's own engine underneath
(`PrismoidBackend`): a JSON codec, units keyed for replay, a row written as each unit completes and an
intent row before every tool body. It is mounted on every built agent and does nothing outside an
open `CallLogScope`, which only a step attempt opens (`active`), so every other run is a plain run;
a run nested inside a tool call (the page reader) runs `detached`, off its step's log.

A unit's key is stable across a replay. A tool call is keyed by its `tool_call_id`, which the
replayed model response that minted it carries again (`!approved` once a person approved it, so the
approved run is not mistaken for the recorded `ApprovalRequired`). Model requests, MCP discovery and
capability operations are keyed by occurrence. A replayed model request is checked against the
recorded request's content fingerprint; a mismatch is `ReplayDiverged`, and the runner fences: the
next generation, told which calls never recorded an outcome. A generation recorded under another
`LOG_VERSION` is fenced the same way when the scope opens, its prompt carried over. Lane-2 events
stay off the log and are suppressed while the run is still replaying.
A replayed write the step's paper holds runs again (`machine_acquire.replays_onto_paper`): paper
ends with its attempt, and its files are the fold of those logged writes.

A run of the attempt is a chain of segments (`RunSegment`): the prompt, then each input one more run
continues it with (a ceiling-stopped attempt's next line, a park's answer). Each
segment's input is logged before it runs and marked once a run of it returned, so every run replays
the same inputs.

A generation keeps the ones below it (`earlier`): an acting call's intent row carries its
`call_digest`, and a later generation's call under the same digest is answered from the result an
earlier one recorded (`earlier_result`), so a fence or a fresh attempt never writes the world twice.

The one window the log cannot close: a death after a provider answered and before its row
committed buys that one request again.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Awaitable, Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Final, Literal, cast

from pydantic import JsonValue
from pydantic_ai import RunContext
from pydantic_ai.durable_exec import (
    JSON_CODEC,
    BaseDurabilityCapability,
    DurabilityEngineSpec,
    DurableOperationId,
    EventStreamHandlerId,
    JournalCallableOperationBackend,
    ModelRequestId,
    RoleBasedOperationConfig,
    ToolsetCallToolId,
    ToolsetValidateToolArgumentsId,
    conversation_id_from_run_id,
    replay_scope,
)
from pydantic_ai.messages import ModelMessage
from pydantic_core import to_json, to_jsonable_python

from prismoid.ai.capabilities.machine_acquire import replays_onto_paper
from prismoid.ai.deps import CallEntry, CallLog, RunKey, TaskDeps
from prismoid.errors import ModelIncapable
from prismoid.schemas.call_log import CallPhase
from prismoid.schemas.ids import StepId

# Bumped when a pinned unit name (agent, toolset id, tool, capability operation) is renamed or
# removed; an added name strands no logged call and needs none. A generation recorded under another
# version is fenced, never replayed against units it did not log.
LOG_VERSION: Final = 1

# A new generation's first row: it is the step's latest generation from the moment it opens (a death
# before its first call re-enters it, never the one before), and it keeps the fence note, if any.
OPENING_UNIT: Final = "segment#0"
_SEGMENT: Final = "segment"

_FINGERPRINTED: Final = ("part_kind", "content", "tool_name", "args", "tool_call_id")
# A tool unit's result shapes (pydantic-ai's `CallToolResult`) that mean the call returned.
_RETURNED: Final = frozenset({"tool_return", "tool_content_result"})

_SCOPE: ContextVar[CallLogScope | None] = ContextVar("prismoid_call_log_scope", default=None)

# A tool call against the log: never logged (live), finished (recorded), or started and never
# finished, either first met now (unknown) or already reported to the model in a later request (told).
type CallState = Literal["live", "recorded", "unknown", "told"]


class ReplayDiverged(RuntimeError):
    """A regenerated model request is not the request recorded at its position."""


@dataclass(frozen=True, slots=True)
class RunSegment:
    """One logged run segment (`segment#<n>`, n >= 1): its input, and whether a run of it returned."""

    n: int
    key: str  # "prompt" | "attempt:<n>" | "gate:<id>" | "in-place:<n>" | "v1"
    line: str
    after: int | None  # the segment whose conversation it continues; None for the first
    returned: bool
    answers: JsonValue = None  # a park's answers to the deferred calls `after` ended on
    history: JsonValue = None  # a first segment's conversation: a park saved before the call log


@dataclass
class CallLogScope:
    """One step attempt's view of its call log: the generation it reads and writes, and its replay."""

    log: CallLog
    key: RunKey
    recorded: dict[tuple[str, str], CallEntry]
    fence_note: str | None = None
    occurrences: Counter[str] = field(default_factory=Counter[str])
    replaying: bool = False  # until the first live unit runs: Lane-2 events are suppressed meanwhile
    requests: int = 0  # model requests this run has made so far, replayed or live
    cut: bool = False  # a unit's body raised since the last model request returned: a death now is unlogged
    segment: int = 0  # the segment running now: each is its own agent run
    earlier: list[Mapping[tuple[str, str], CallEntry]] = field(
        default_factory=list[Mapping[tuple[str, str], CallEntry]]
    )  # every lower generation's entries: what a re-run must not act out again
    digests: dict[str, str] = field(default_factory=dict[str, str])  # an acting call's `call_digest`, by id

    @classmethod
    async def open(
        cls, log: CallLog, step: StepId, *, fresh: bool, version: int = LOG_VERSION
    ) -> CallLogScope:
        """The latest generation (a crash re-enters it), or the next one when `fresh` or fenced."""
        latest = await log.latest(step)
        if latest.key is None:
            return cls(log, RunKey(step, 0, version), {})
        following = RunKey(step, latest.key.generation + 1, version)
        if fresh or latest.key.log_version != version:
            scope = cls(log, following, {}, earlier=[*latest.earlier, latest.entries])
            await scope.begin(None if fresh else unknown_outcomes(latest.entries))
            if not fresh:
                await scope.carry(cls(log, latest.key, dict(latest.entries)).segments())
            return scope
        opening = latest.entries.get((OPENING_UNIT, CallPhase.RESULT))
        note = _note_of(opening.payload) if opening is not None else None
        scope = cls(log, latest.key, dict(latest.entries), fence_note=note, earlier=[*latest.earlier])
        scope.rewind()
        return scope

    async def fence(self) -> None:
        """Start the next generation, noted with every call whose outcome this one never recorded."""
        unknown = unknown_outcomes(self.recorded)
        self.earlier.append(self.recorded)
        self.key = RunKey(self.key.step, self.key.generation + 1, self.key.log_version)
        self.recorded = {}
        self.rewind()
        await self.begin(unknown)

    async def carry(self, fenced: Sequence[RunSegment]) -> None:
        """A version-fenced generation's root, noted, as this one's first segment: a re-entry, a continue
        or a park's answer runs again from the prompt."""
        root = next((s for s in fenced if s.after is None), None)
        if root is not None:
            line = root.line if self.fence_note is None else f"{root.line}\n\n{self.fence_note}"
            await self.append(root.key, line, after=None, answers=root.answers, history=root.history)

    async def begin(self, note: str | None) -> None:
        """Write the generation's opening row, keeping its fence note for a later re-entry."""
        self.fence_note = note
        await self.write(CallEntry(OPENING_UNIT, CallPhase.RESULT, {"note": note}))

    def rewind(self) -> None:
        """Start a run of the attempt from the top of its generation: what is logged replays again."""
        self.occurrences, self.requests, self.cut = Counter[str](), 0, False
        self.replaying = any(
            phase == CallPhase.RESULT and _segment_n(unit) is None for unit, phase in self.recorded
        )

    async def write(self, entry: CallEntry) -> None:
        """Log one row of this generation, once; a later run of the attempt replays it."""
        if (entry.unit, entry.phase) in self.recorded:
            return
        await self.log.record(self.key, entry)
        self.recorded[(entry.unit, entry.phase)] = entry

    def segments(self) -> list[RunSegment]:
        """The generation's run segments, in order."""
        found: list[RunSegment] = []
        for (unit, phase), entry in self.recorded.items():
            if phase != CallPhase.INTENT or (n := _segment_n(unit)) is None:
                continue
            fields = entry.payload if isinstance(entry.payload, dict) else {}
            after = fields.get("after")
            found.append(
                RunSegment(
                    n,
                    str(fields.get("key")),
                    str(fields.get("line")),
                    after if isinstance(after, int) else None,
                    returned=self.recorded.get((unit, CallPhase.RESULT)) is not None,
                    answers=fields.get("answers"),
                    history=fields.get("history"),
                )
            )
        return sorted(found, key=lambda segment: segment.n)

    async def append(
        self,
        key: str,
        line: str,
        *,
        after: int | None,
        answers: JsonValue = None,
        history: JsonValue = None,
    ) -> RunSegment:
        """Log a new segment's input before it runs."""
        n = max((segment.n for segment in self.segments()), default=0) + 1
        payload = {"key": key, "line": line, "after": after}
        payload |= {k: v for k, v in (("answers", answers), ("history", history)) if v is not None}
        await self.write(CallEntry(f"{_SEGMENT}#{n}", CallPhase.INTENT, cast(JsonValue, payload)))
        return RunSegment(n, key, line, after, returned=False, answers=answers, history=history)

    async def returned(self, segment: RunSegment) -> None:
        """Mark a segment whose run returned: a later run may continue its conversation."""
        await self.write(CallEntry(f"{_SEGMENT}#{segment.n}", CallPhase.RESULT, None))

    def call_state(self, tool_call_id: str, *, approved: bool) -> CallState:
        """What the log holds for this tool call; `told` once a later model request was recorded."""
        key = f"@{tool_call_id}{'!approved' if approved else ''}"
        unit = next((unit for unit, _ in self.recorded if unit.endswith(key)), None)
        if unit is None:
            return "live"
        if self.recorded.get((unit, CallPhase.RESULT)) is not None:
            return "recorded"
        answered = sum(
            1 for e in self.recorded.values() if e.phase == CallPhase.RESULT and e.fingerprint is not None
        )
        return "told" if answered > self.requests else "unknown"

    def done_before(self, digest: str) -> JsonValue | None:
        """What an earlier generation's finished call under `digest` returned, or None."""
        for entries in self.earlier:
            for (unit, phase), entry in entries.items():
                fields = entry.payload if isinstance(entry.payload, dict) else {}
                if phase != CallPhase.INTENT or fields.get("digest") != digest:
                    continue
                done = entries.get((unit, CallPhase.RESULT))
                result = done.payload if done is not None and isinstance(done.payload, dict) else {}
                if result.get("kind") in _RETURNED:
                    return result.get("result")
        return None

    def unit_of(self, operation_id: DurableOperationId, name: str, cache_key: tuple[object, ...]) -> str:
        """The unit's replay-stable key: a tool call by its id, anything else by occurrence."""
        if isinstance(operation_id, ToolsetCallToolId) and (ctx := _context_of(cache_key)) is not None:
            approved = "!approved" if ctx.tool_call_approved else ""
            return f"{name}@{ctx.tool_call_id}{approved}"
        seen = self.occurrences[name]
        self.occurrences[name] += 1
        return f"{name}#{seen}"


def unknown_outcomes(entries: Mapping[tuple[str, str], CallEntry]) -> str | None:
    """The note a fenced attempt reads: every call that started and never recorded an outcome."""
    calls = [
        entry.payload
        for (unit, phase), entry in sorted(entries.items())
        if phase == CallPhase.INTENT
        and _segment_n(unit) is None
        and entries.get((unit, CallPhase.RESULT)) is None
    ]
    if not calls:
        return None
    named = "".join(f"\n- {_called(payload)}" for payload in calls)
    return (
        "An earlier run of this step was interrupted, and these calls never reported an outcome. "
        f"They may have partly happened, so check the current state before repeating any:{named}"
    )


def _segment_n(unit: str) -> int | None:
    """A run segment's number (`segment#<n>`, n >= 1), or None for any other unit."""
    name, _, n = unit.rpartition("#")
    return int(n) if name == _SEGMENT and n != "0" else None


def _called(payload: JsonValue) -> str:
    """One unfinished call, as `tool(args)`."""
    fields = payload if isinstance(payload, dict) else {}
    return f"{fields.get('tool')}({to_json(fields.get('args')).decode()})"


def _note_of(payload: JsonValue) -> str | None:
    """The note a fenced generation kept."""
    note = payload.get("note") if isinstance(payload, dict) else None
    return note if isinstance(note, str) else None


def _refolds(operation_id: DurableOperationId, cache_key: tuple[object, ...], payload: JsonValue) -> bool:
    """A replayed tool call that returned and that the step's paper must apply again."""
    if not isinstance(operation_id, ToolsetCallToolId) or (ctx := _context_of(cache_key)) is None:
        return False
    returned = isinstance(payload, dict) and payload.get("kind") in _RETURNED
    return returned and replays_onto_paper(ctx.deps, str(cache_key[0]))


def _context_of(cache_key: tuple[object, ...]) -> RunContext[Any] | None:
    """The tool call's run context, where its id and approval ride."""
    contexts = (cast(RunContext[Any], k) for k in cache_key if isinstance(k, RunContext))
    return next(contexts, None)


def request_fingerprint(messages: Sequence[ModelMessage]) -> str:
    """Content only: part kinds, text, tool names, args and ids (timestamps and run ids differ on replay)."""
    reduced: list[dict[str, Any]] = []
    for message in messages:
        for part in message.parts:
            dumped = cast(dict[str, Any], to_jsonable_python(part))
            reduced.append({k: dumped[k] for k in _FINGERPRINTED if k in dumped})
    return hashlib.sha256(to_json(reduced)).hexdigest()[:16]


def call_digest(tool: str, args: Mapping[str, Any]) -> str:
    """A call's identity across generations: its model-facing name and args, hashed (sha256 hex)."""
    canonical = json.dumps(
        [tool, to_jsonable_python(args, fallback=str)], sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def earlier_result(tool_call_id: str, digest: str) -> JsonValue | None:
    """An earlier generation's result for the same acting call, or None (outside a step attempt too);
    either way the call's intent row carries `digest`, so a later generation finds it."""
    scope = _SCOPE.get()
    if scope is None:
        return None
    scope.digests[tool_call_id] = digest
    return scope.done_before(digest)


def call_state(tool_call_id: str, *, approved: bool) -> CallState:
    """The open scope's state of a tool call; outside a step attempt every call is live."""
    scope = _SCOPE.get()
    return "live" if scope is None else scope.call_state(tool_call_id, approved=approved)


@contextmanager
def active(scope: CallLogScope) -> Generator[None]:
    """Run the code inside on `scope`: the only setter of the scope, and replayed telemetry stays out."""
    token = _SCOPE.set(scope)
    try:
        with replay_scope(is_replaying=lambda: scope.replaying):
            yield
    finally:
        _SCOPE.reset(token)


@contextmanager
def detached() -> Generator[None]:
    """Run the code inside on no call log: a run nested in a tool call logs nothing into its step's."""
    token = _SCOPE.set(None)
    try:
        yield
    finally:
        _SCOPE.reset(token)


class PrismoidBackend(JournalCallableOperationBackend[None]):
    """Each durable unit as call-log rows: replayed when recorded, else run and written."""

    def __init__(self, agent_name: str, default_model_id: str | None) -> None:
        config = RoleBasedOperationConfig[None](model=None, event=None, capability=None, tool=None)
        super().__init__(agent_name=agent_name, default_model_id=default_model_id, config=config)

    async def execute(
        self,
        *,
        operation_id: DurableOperationId,
        name: str,
        body: Callable[[], Awaitable[object]],
        cache_key: tuple[object, ...],
        config: None,
    ) -> object:
        """Replay the unit's recorded result, or run it between its intent and its result rows."""
        scope = _SCOPE.get()
        if scope is None or isinstance(operation_id, ToolsetValidateToolArgumentsId):
            return await body()  # argument validation is deterministic: it reruns, never logged
        if isinstance(operation_id, EventStreamHandlerId):
            return JSON_CODEC.dump(type(None), None) if scope.replaying else await body()
        unit = scope.unit_of(operation_id, name, cache_key)
        fingerprint = None
        if isinstance(operation_id, ModelRequestId):
            fingerprint = request_fingerprint(cast(Sequence[ModelMessage], cache_key[1]))
            scope.requests += 1
        if (done := scope.recorded.get((unit, CallPhase.RESULT))) is not None:
            if fingerprint is not None and done.fingerprint not in (None, fingerprint):
                raise ReplayDiverged(f"{unit}: recorded {done.fingerprint}, regenerated {fingerprint}")
            if _refolds(operation_id, cache_key, done.payload):
                await body()  # a write paper holds, applied again: paper ends with its attempt
            return done.payload
        scope.replaying = False
        if isinstance(operation_id, ToolsetCallToolId):
            intent = {"tool": cache_key[0], "args": to_jsonable_python(cache_key[1], fallback=str)}
            if (ctx := _context_of(cache_key)) is not None and ctx.tool_call_id in scope.digests:
                intent["digest"] = scope.digests[ctx.tool_call_id]
            await scope.write(CallEntry(unit, CallPhase.INTENT, cast(JsonValue, intent)))
        try:
            payload = cast(JsonValue, await body())
        except BaseException:
            scope.cut = True
            raise
        if fingerprint is not None:
            scope.cut = False
        await scope.write(CallEntry(unit, CallPhase.RESULT, payload, fingerprint))
        return payload


class PrismoidDurability(BaseDurabilityCapability[TaskDeps]):
    """The call log as the run's durable engine; inert outside an open scope."""

    engine_spec = DurabilityEngineSpec(
        engine_name="Prismoid",
        durable_unit_noun="call",
        durable_container_noun="attempt",
        codec=JSON_CODEC,
        serialization_failure=lambda exc: ModelIncapable(f"a call could not be logged: {exc}"),
        # A toolset handed to one run is not wrapped by the engine, so it would run unlogged.
        unsupported_runtime_toolset_kinds=frozenset({"function", "mcp", "dynamic"}),
        toolset_lifecycles={
            "function": "enter-always",
            "mcp": "enter-in-durable-unit",
            "dynamic": "enter-never",
        },
        # Prismoid's cancel is terminal: a cancelled attempt is never replayed (ADR-089).
        accepts_cancellation_token=True,
    )

    @property
    def in_durable_context(self) -> bool:
        """Whether a step attempt's scope is open."""
        return _SCOPE.get() is not None

    def get_durable_operation_backend(self) -> PrismoidBackend:
        """The call-log backend under this agent's name."""
        return PrismoidBackend(self.name, self.default_model_id)

    def get_default_run_id(self) -> str | None:
        """A run id stable across a replay: the step, its generation and the segment running."""
        scope = _SCOPE.get()
        return None if scope is None else f"{scope.key.step}~{scope.key.generation}~{scope.segment}"

    def get_default_conversation_id(self, run_id: str) -> str | None:
        """One conversation per generation, whichever segment runs."""
        scope = _SCOPE.get()
        return (
            None if scope is None else conversation_id_from_run_id(f"{scope.key.step}~{scope.key.generation}")
        )
