# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Lane 2 - the wire. Ephemeral frames that never touch a table.

Two lanes, one socket. Lane 1 is **the record**: durable `TaskEvent`s with a per-task `seq`,
replayable, the flight recorder. Lane 2 is **the wire**: deltas the user watches, keyed by step,
anchored to the last durable seq - and **never persisted per frame**.

**The reconciliation rule is what makes losing them boring**: every Lane-2 stream terminates
in Lane-1 durability. Message deltas end in a durable `step.narration` carrying the full markdown;
artifact deltas end in the published artifact itself, plus durable `artifact.snapshot` checkpoints
every ~8 KB during a long write. So a reconnect replays Lane 1, renders the latest snapshot, and
picks the deltas back up - and a frame lost in a disconnect never mattered, because its terminal
state was durable all along.

They ride the same LISTEN/NOTIFY fan-out as Lane 1, because boring beats clever: one hub, one
socket, one thing to operate. A dedicated pub/sub can replace it at scale without touching a single
emitter - which is the whole reason the hub sits behind an interface.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Awaitable, Callable
from typing import Literal, Protocol

import logfire
import orjson
from sqlalchemy import text

from prismoid.ai.tools import labels
from prismoid.db.base import utcnow
from prismoid.db.models.event import NOTIFY_CHANNEL
from prismoid.db.session import session_scope
from prismoid.schemas.conversation import ChatLiveEvent
from prismoid.schemas.events import Activity, LiveEvent, Progress
from prismoid.schemas.ids import ConversationId, ProjectId, StepId, TaskId

# What rides Lane 2: a step's frames, or a conversation reply's - same hub, same
# NOTIFY, keyed by the id each carries.
Frame = LiveEvent | ChatLiveEvent

# Stream budget: ≤10 frames/s per channel, ≤4 KB per frame - under Postgres's NOTIFY payload ceiling, so
# Lane 2 can ride the same socket as Lane 1 without a second transport to operate.
MAX_FRAME_BYTES = 4096
MIN_FRAME_INTERVAL_S = 0.1

# The no-dead-air rule. A client watching a step must see an honest signal at least this
# often. **Honest** is the load-bearing word: a spinner is not a signal, it is a promise that
# something is happening, and a spinner over a dead worker is a lie that costs the user ten minutes.
DEAD_AIR_S = 10.0


async def notify(event: Frame) -> None:
    """Publish one ephemeral frame. **No row, no seq, no replay** - that is the point of Lane 2.

    The batch notifier carries it: hundreds of deltas a second, flushed in one
    transaction per ~20 ms tick, so Postgres's commit-time NOTIFY lock is taken once per flush
    instead of once per frame - the difference between a lock convoy at a few thousand writes/s
    and comfortable headroom. Losing a frame is survivable by design (every stream ends in
    Lane-1 durability), which is exactly why this path may batch and Lane 1 may not.

    **Fallback**: if the notifier is not running (the API process, a test), send it directly in a
    short own transaction - `NOTIFY` is delivered on *commit*, and the step's transaction will not
    commit for another minute, so a delta must not ride it.
    """
    payload = event.model_dump_json()
    if len(payload) > MAX_FRAME_BYTES:  # pragma: no cover - the coalescer bounds this
        logfire.warn("dropping an oversized live frame ({n} bytes)", n=len(payload))
        return

    from prismoid.operations.notifier import notifier

    if notifier.enqueue(payload):
        return

    async with session_scope(platform=True) as session:  # a notify reads and writes no tenant row
        await session.execute(
            text("select pg_notify(:channel, :payload)"),
            {"channel": NOTIFY_CHANNEL, "payload": payload},
        )
        await session.commit()


class Wire:
    """One step's Lane-2 channel: the frame budget, the anchor, and **when it last said anything**.

    `after_seq` is the anchor: every ephemeral frame carries the last *durable* seq the step
    emitted, so a client can order deltas against the record without a clock and without guessing.
    Without it, a reconnecting client has a pile of deltas and no idea where they belong.

    The "when it last spoke" part is what the heartbeat reads. A `progress` frame fired while tokens
    are already streaming is noise; one fired into ten seconds of genuine silence is the only thing
    between the user and a blank screen.
    """

    def __init__(self, task_id: TaskId, step_id: StepId, project_id: ProjectId | None) -> None:
        self.task_id = task_id
        self.step_id = step_id
        self.project_id = project_id
        self.after_seq = 0
        self.started = time.monotonic()
        self.last_sent = 0.0

    def anchor(self, seq: int) -> None:
        """The step just wrote a durable event. Everything ephemeral from here hangs off it."""
        self.after_seq = seq

    async def send(self, event: Frame) -> None:
        now = time.monotonic()
        if now - self.last_sent < MIN_FRAME_INTERVAL_S:
            return  # dropped, deliberately: the terminal state is durable (see the module docstring)
        self.last_sent = now
        await notify(event)

    async def say(self, event: Frame) -> None:
        """Send without the drop gate: a `message.delta` is an append-only increment and an `activity` end must
        not strand its row; both are paced upstream."""
        self.last_sent = time.monotonic()  # the heartbeat should count this as speech
        await notify(event)

    def quiet_for(self) -> float:
        return time.monotonic() - (self.last_sent or self.started)

    def elapsed(self) -> float:
        return time.monotonic() - self.started


class ChatWire:
    """A conversation reply's Lane-2 channel: the step's `Wire`, one lane over -
    keyed by the conversation, anchored on the last durable MESSAGE seq. Unpaced like `say`
    (the tap coalesces upstream); no heartbeat of its own - the first delta is seconds away
    and the web's own "is replying…" line stands in until then."""

    def __init__(self, conversation_id: ConversationId, after_seq: int) -> None:
        self.conversation_id = conversation_id
        self.after_seq = after_seq
        self.last_sent = 0.0

    async def say(self, event: Frame) -> None:
        self.last_sent = time.monotonic()
        await notify(event)


class Speaker(Protocol):
    """What `emitter` needs of a wire: an anchor and a mouth."""

    after_seq: int

    async def say(self, event: Frame) -> None: ...


EmitLive = Callable[[Frame], Awaitable[None]]


def emitter(wire: Speaker) -> EmitLive:
    """The `emit_event` an agent is handed. It cannot reach a database and does not know there is
    one - so the **anchor is stamped here**, not in `ai/`: the engine builds frames with
    `after_seq=0` and this closure pins each one to the last durable seq before it goes out."""

    async def emit(event: Frame) -> None:
        await wire.say(event.model_copy(update={"after_seq": wire.after_seq}))

    return emit


class ActivityFact(Protocol):
    """A narrator's voice: one machinery fact onto the wire, best-effort by construction."""

    def __call__(
        self, phase: Literal["start", "end"], label: str, *, started: float | None = None
    ) -> Awaitable[None]: ...


def activity_emitter(
    wire: Wire, *, kind: Literal["engine", "phase"] = "engine", tool: str = "", wait_s: int | None = None
) -> ActivityFact:
    """The ONE operations-side `Activity` constructor (the progress tree's machinery rows).

    Three narrators used to build this envelope by hand - the engine narrator, the system-publish
    fact, and the phase fact - and drifted on anchor, project and clamp. They all speak through here
    now: the emitter stamps the anchor, `labels.clamp` bounds every label to the wire width, and
    best-effort is structural (`suppress` - narration must never cost a step). `wait_s` rides every
    `start` frame: the bound on the wait this narrator's rows stand for (a Machine's phases)."""
    say = emitter(wire)

    async def fact(phase: Literal["start", "end"], label: str, *, started: float | None = None) -> None:
        with contextlib.suppress(Exception):
            await say(
                Activity(
                    task_id=wire.task_id,
                    step_id=wire.step_id,
                    project_id=wire.project_id,
                    at=utcnow(),
                    after_seq=0,  # stamped by the emitter
                    channel=f"{wire.step_id}:activity",
                    kind=kind,
                    phase=phase,
                    tool=tool,
                    label=labels.clamp(label),
                    duration_ms=int((time.monotonic() - started) * 1000) if started else 0,
                    wait_s=wait_s if phase == "start" else None,
                )
            )

    return fact


class PhaseFact(Protocol):
    """A pre-step narrator's voice: one phase fact onto the wire, naming WHO is working."""

    def __call__(self, phase: Literal["start", "end"], label: str, *, agent: str = "") -> Awaitable[None]: ...


def phase_narrator(task_id: TaskId, *, project_id: ProjectId | None = None, after_seq: int = 0) -> PhaseFact:
    """Bind one task's `kind="phase"` voice, the pre-step machinery's face; `agent` lands on the frame's
    `tool`. Best-effort: a NOTIFY hiccup never costs intake or planning."""

    async def fact(phase: Literal["start", "end"], label: str, *, agent: str = "") -> None:
        with contextlib.suppress(Exception):
            await notify(
                Activity(
                    task_id=task_id,
                    project_id=project_id,
                    at=utcnow(),
                    after_seq=after_seq,
                    channel=f"{task_id}:phase",
                    kind="phase",
                    phase=phase,
                    tool=agent,
                    label=labels.clamp(label),
                )
            )

    return fact


async def heartbeat(wire: Wire, phase: str = "thinking") -> None:
    """**No dead air**. Runs beside the step; cancelled when it ends.

    If nothing else has been said for ten seconds, say something true: how long it has been going.
    Not a spinner - **a spinner is a promise that something is happening, and a spinner over a dead
    worker is a lie** that costs the user ten minutes before they think to check.
    """
    import asyncio

    while True:
        await asyncio.sleep(1.0)
        if wire.quiet_for() < DEAD_AIR_S:
            continue
        await wire.send(
            Progress(
                task_id=wire.task_id,
                step_id=wire.step_id,
                project_id=wire.project_id,
                at=utcnow(),
                after_seq=wire.after_seq,
                channel=f"{wire.step_id}:progress",
                phase=phase,
                elapsed_s=round(wire.elapsed(), 1),
                detail="working",
            )
        )


def frame_of(payload: bytes) -> dict[str, object]:  # pragma: no cover - used by tests only
    return dict(orjson.loads(payload))
