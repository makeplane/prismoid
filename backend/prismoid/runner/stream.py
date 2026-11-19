# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`prismoid tasks events --follow` - the terminal flight recorder.

SSE under the hood, because that is what the product's own docs tell a user to reach for
(`curl -N …`), and the CLI should not be a special case with a private transport. Lane 1 (the durable
record) always.
"""

from __future__ import annotations

import json
import secrets
import sys
from collections.abc import Callable, Iterable, Iterator
from decimal import Decimal, InvalidOperation
from typing import Any, cast

import typer

from prismoid.runner import client, questions
from prismoid.runner.terminal import printable
from prismoid.schemas import task_states
from prismoid.schemas.events import LIVE_TYPES

# The event types worth a colour. Everything else prints plainly rather than being hidden - a
# flight recorder that decides what you don't need to see is not a flight recorder.
_COLOURS: dict[str, str] = {
    "task.created": "cyan",
    "task.planned": "cyan",
    "task.delivered": "green",
    "escalation.raised": "red",
    "step.state": "blue",
    "verification.signed": "green",
    "artifact.produced": "magenta",
    "usage.recorded": "bright_black",
    # Presence is ambient: who is here, not what happened.
    "presence.join": "bright_black",
    "presence.here": "bright_black",
    "presence.leave": "bright_black",
    "presence.typing": "bright_black",
}

TERMINAL = {"task.delivered", "escalation.raised"}
# A cancel or a failure is recorded as `task.status{cancelled|failed}` (nothing emits
# `task.cancelled`), so a follow that waited only for the events above sat on a stopped task forever.
_STOPPED = frozenset(str(s) for s in task_states.TERMINAL)


def task_stopped(payload: dict[str, Any]) -> bool:
    """Whether this durable frame says the task itself has stopped (delivered, failed or cancelled).
    The one answer to that question: the CLI's follow and the eval harness's replay both ask it."""
    if payload.get("type") == "task.delivered":
        return True
    return payload.get("type") == "task.status" and payload.get("status") in _STOPPED


def ends_follow(event_name: str | None, payload: dict[str, Any]) -> bool:
    """Whether this frame means the task has stopped, so `--follow`/`--wait` should return. An
    escalation ends it too."""
    return event_name in TERMINAL or task_stopped(payload)


# How long `prismoid do --wait` asks whether a hand-off is on the user's own computer before it shows
# the gate as a line, as it always did: never a stalled stream.
DEVICE_READ_S = 5.0

# Where a steer took hold (its `SafePoint`), as the line after "applied to '<step>'" says it.
# `boundary` is a pause's only, and reads in the `pause.applied` line.
APPLIED_WORDS: dict[str, str] = {
    "before_model": "before its next model call",
    "before_tool": "before its next tool call",
    "final_answer": "before it finished",
    "approval": "(superseded the approval it was waiting on)",
    "replan": "(re-planned the remaining steps)",
    "rerun": "(it is going again with it)",
    "interrupt": "(cut a reply short)",
}

# What a teammate's typing is for (`presence.typing.mode`), in the web's words.
_TYPING = {"steer": "steering", "answer": "answering"}


def _who(actor: Any) -> str:
    """A person's name as it reaches YOUR terminal. Another member's display name
    is theirs to choose, so it is printable or nothing - never an escape sequence - and no more
    than the 60 the prompts cap it at."""
    if not isinstance(actor, dict):
        return ""
    name = cast(dict[str, Any], actor).get("name")
    return printable(str(name or ""))[:60]


# A line's body, per event type: (event, who did it) -> the words after the kind column. A new
# event type is one row here; a type with no row prints its kind and seq with an empty body.
Body = Callable[[dict[str, Any], str], object]


def _step_state(event: dict[str, Any], _name: str) -> str:
    body = f"{event.get('title') or event.get('agent') or ''} → {event.get('state')}"
    if event.get("outcome") not in (None, "done"):  # absent on older frames
        why = f": {event['blocked_by']}" if event.get("blocked_by") else ""
        body += f" ({event['outcome']}{why})"
    return body


def _steer_applied(event: dict[str, Any], _name: str) -> str:
    title = event.get("step_title") or "the plan"
    words = APPLIED_WORDS.get(str(event.get("safe_point")), "")
    whose = f"{event['author']}'s" if event.get("author") else "a"  # older frames name no one
    return f"{whose} steer applied to '{title}' {words}".rstrip()


def _pause_applied(event: dict[str, Any], _name: str) -> str:
    where = "at the boundary" if event.get("safe_point") == "boundary" else "before its next tool call"
    return f"paused {where}"


def _presence(event: dict[str, Any], who: str) -> str:
    return f"{who or 'someone'}{'' if event.get('editor', True) else ' (watching)'}"


def _typing(event: dict[str, Any], who: str) -> str:
    name = who or "someone"
    typing = _TYPING.get(str(event.get("mode")), "typing")
    return f"{name} {typing}…" if event.get("on") else f"{name} stopped"


def _screen_state(event: dict[str, Any], _name: str) -> str:
    holder = _who(event.get("holder")) if event.get("holder") else ""
    until = f" until {event['until']}" if event.get("until") else ""
    return f"{holder or 'nobody'}{' (private)' if event.get('private') else ''}{until}"


def _clip(text: object, limit: int) -> str:
    words = str(text or "")
    return words if len(words) <= limit else words[: limit - 1] + "…"


def _took(ms: object) -> str:
    """Wall time as a person reads it: 8ms, 1.8s, 2m 05s. Nothing for a call that never ran."""
    if not isinstance(ms, int) or isinstance(ms, bool) or ms <= 0:
        return ""
    if ms < 1000:
        return f"{ms}ms"
    if ms < 60_000:
        return f"{ms // 1000}s" if ms % 1000 == 0 else f"{ms / 1000:.1f}s"
    return f"{ms // 60_000}m {ms % 60_000 // 1000:02d}s"


def _size(n: object) -> str:
    if not isinstance(n, int) or isinstance(n, bool):
        return ""
    if n < 1024:
        return f"{n} B"
    return f"{n / 1024:.1f} KB" if n < 1024 * 1024 else f"{n / 1024 / 1024:.1f} MB"


def _count(n: int) -> str:
    return str(n) if n < 1000 else f"{n / 1000:.1f}k"


def _tool_called(event: dict[str, Any], _name: str) -> str:
    """Which tool, on what, how long, and why it failed. The summary is the journal's own (a host
    and path, a command line, a file), never the call's arguments: those can carry secrets."""
    line = " ".join(p for p in (str(event.get("tool") or "?"), _clip(event.get("summary"), 120)) if p)
    if event.get("ok") is False:
        line += f" - failed: {_clip(event.get('denied_reason') or 'no reason given', 200)}"
    took = _took(event.get("duration_ms"))
    return f"{line} · {took}" if took else line


def _task_planned(event: dict[str, Any], _name: str) -> str:
    steps = len((event.get("plan") or {}).get("steps") or [])
    line = f"{event.get('route') or 'standard'} · {steps} step{'' if steps == 1 else 's'}"
    line += f" ({event['route_source']})" if event.get("route_source") else ""
    line += " · computer" if event.get("machine") == "computer" else ""
    return line + (f" · fallback: {_clip(event['fallback'], 200)}" if event.get("fallback") else "")


def _step_prompt(event: dict[str, Any], _name: str) -> str:
    """Who got the prompt and on which model, never its text: a step prompt can carry the task
    creator's private memory. `--json` keeps the text, for whoever the server shows it to."""
    model = str(event.get("model_ref") or "").rpartition(":")[2]
    line = f"attempt {event.get('attempt', '?')} · {event.get('agent') or '?'}"
    line += f" on {model}" if model else ""
    return line + (" (clipped)" if event.get("truncated") else "")


# How a bill's native units read after the price, in `Unit` order. A call is one request, always.
_UNITS = (
    ("input_token", "in"),
    ("output_token", "out"),
    ("cache_read_token", "cache read"),
    ("cache_write_token", "cache write"),
    ("reasoning_token", "reasoning"),
    ("search", "search"),
    ("image", "image"),
    ("character", "chars"),
)


def _price(cost: object) -> str:
    """To the hundredth of a cent; the ledger keeps the exact decimal, and `--json` prints it."""
    try:
        usd = Decimal(str(cost))
    except InvalidOperation:
        return f"${cost}"
    if usd == 0:
        return "$0"
    return "<$0.0001" if usd < Decimal("0.0001") else f"${usd:.4f}"


def _usage(event: dict[str, Any], _name: str) -> str:
    """The model, the price, and what it billed: tokens, searches, a Machine's warm time."""
    units = cast(dict[str, Any], event.get("units") or {})
    billed = [f"{_count(n)} {word}" for key, word in _UNITS if isinstance(n := units.get(key), int) and n > 0]
    if isinstance(secs := units.get("second"), int) and secs > 0:
        billed.append(_took(secs * 1000))
    line = f"{event.get('model')} {_price(event.get('cost_usd'))}"
    return line + (f" · {', '.join(billed)}" if billed else "")


def _artifact(event: dict[str, Any], _name: str) -> str:
    line = f'{event.get("artifact_type") or "file"} "{_clip(event.get("title"), 120)}"'
    line += f" · {size}" if (size := _size(event.get("bytes"))) else ""
    return line + (" (working)" if event.get("role") == "working" else "")


def _remembered(event: dict[str, Any], _name: str) -> str:
    """How many memories were kept, never their words: a memory is its owner's to read, and a
    terminal line ends up in a CI log."""
    if event.get("skipped"):
        return f"none: a {event['skipped']} task keeps no memory"
    count = event.get("count") or 0
    kept = f"kept {count} memor{'y' if count == 1 else 'ies'}"
    return f"{event['bot']} {kept}" if event.get("bot") else kept


def _context(event: dict[str, Any], _name: str) -> str:
    tokens = event.get("total_tokens")
    memories = len(event.get("memory_ids") or [])
    line = f"{_count(tokens)} tokens" if isinstance(tokens, int) else "? tokens"
    return line + f" · {memories} memor{'y' if memories == 1 else 'ies'}"


def _machine_slept(event: dict[str, Any], _name: str) -> str:
    after = event.get("after_s")
    line = str(event.get("reason") or "")
    line += f" · after {_took(after * 1000)}" if isinstance(after, int) and after > 0 else ""
    return line + (" · /work was over the cap and not kept" if event.get("dropped_bytes") else "")


_BODIES: dict[str, Body] = {
    "task.created": lambda e, _: e.get("goal", ""),
    "task.continued": lambda e, _: str(e.get("message", ""))[:200],
    "task.dequeued": lambda e, _: f"turn {e.get('turn', 1)}",
    "task.renamed": lambda e, _: e.get("title", ""),
    "task.planned": _task_planned,
    # The status the line is about: "task.status (Ada) paused", "(Bob) running".
    "task.status": lambda e, _: e.get("status", ""),
    "machine.created": lambda e, _: f"{e.get('profile') or '?'} profile",
    "machine.slept": _machine_slept,
    "step.state": _step_state,
    "step.prompt": _step_prompt,
    "step.narration": lambda e, _: str(e.get("markdown", ""))[:200],
    "tool.called": _tool_called,
    "usage.recorded": _usage,
    "artifact.produced": _artifact,
    "artifact.revised": lambda e, _: f"{_artifact(e, _)} · revision {e.get('revision', '?')}",
    "context.assembled": _context,
    "memory.written": _remembered,
    "bot_memory.written": _remembered,
    "verification.signed": lambda e, _: f"{e.get('verdict')} ({e.get('suite')})",
    "escalation.raised": lambda e, _: _clip((e.get("diagnosis") or {}).get("what_failed"), 200),
    "task.delivered": lambda e, _: str(e.get("summary", ""))[:200],
    "plan.edited": lambda e, _: str((e.get("edit") or {}).get("reason", ""))[:200],
    "steer.applied": _steer_applied,
    "steer.rejected": lambda e, _: e.get("reason", ""),
    "steer.retracted": lambda e, _: "retracted",
    "pause.applied": _pause_applied,
    # A teammate answers with `prismoid gates resolve <gate>`.
    "gate.opened": lambda e, _: f"{e.get('title') or ''} · gate {e.get('gate_id')}",
    # The action only: an answer's content can be private.
    "gate.resolved": lambda e, _: f"{e.get('action')} · gate {e.get('gate_id')}",
    "presence.join": _presence,
    "presence.here": _presence,
    "presence.leave": _presence,
    "presence.typing": _typing,
    "screen.action": lambda e, _: f"{e.get('verb', '')} {e.get('host', '')}{e.get('path', '')}",
    "screen.state": _screen_state,
}


def _render(event: dict[str, Any]) -> str:
    kind = event.get("type", "?")
    seq = event.get("seq", "-")  # presence frames are live: no seq, no `id:`
    who = _who(event.get("actor"))
    body = _BODIES.get(str(kind), lambda e, _: "")(event, who)
    # Who did it rides the kind column. Presence already names its person in the body.
    label = kind if not who or str(kind).startswith("presence.") else f"{kind} ({who})"
    # A body is data somebody wrote - in a shared Project, any member (a goal, a steer's narration,
    # a summary): it reaches this terminal printable or not at all.
    return f"[{seq:>3}] {label:<22} {printable(str(body))}"


# A step's live deltas: `activity`, `progress`, `message.*`, `artifact.delta`. Presence and the
# screen are live too, but state, not a preview of the record, so they keep their lines.
_DELTAS = frozenset(str(c.model_fields["type"].default) for c in LIVE_TYPES)


# The two sanitizers, public for the other commands that print a server-supplied name
# (`printable` is public by its own name).
person_name = _who


def _news(payload: dict[str, Any], own_conn: str, present: set[str]) -> bool:
    """Whether a frame earns a line in a terminal. The record always does. A step's live deltas
    never do: each stream ends in a durable frame that says the same thing (a tool's start and end
    are its `tool.called`), and a terminal cannot fold the two into one line as the web does.
    Presence is state, and every stream hears every frame on its task, its own included: this
    terminal's own frames are not news to it, and a `here` is a heartbeat, news only the first time
    its connection is seen (it is how a newcomer learns who was already in the room). `--json`
    never asks: raw is raw."""
    kind = str(payload.get("type", ""))
    if kind in _DELTAS:
        return False
    if not kind.startswith("presence."):
        return True
    conn = str(payload.get("conn", ""))
    if conn == own_conn:
        return False
    if kind == "presence.leave":
        present.discard(conn)
        return True
    first_sight = conn not in present
    present.add(conn)
    return kind != "presence.here" or first_sight


def _device_of(task_id: str) -> str | None:
    """The device name ("" when unknown) when the caller's own task runs on their computer, else
    None: a cloud Machine, a task someone else created (only its creator says Done), or a read that
    was refused, timed out or could not connect - the gate is then a line, as it always was."""
    try:
        task = client.request(
            "GET", f"/v1/tasks/{task_id}", as_json=False, raise_refused=True, timeout=DEVICE_READ_S
        )
    except client.Refused:
        return None
    machine = task.get("machine") or {}
    if machine.get("driver") != "device" or not task.get("is_creator"):
        return None
    return str(machine.get("device_name") or "")


def follow_events(
    task_id: str, *, since: int = 0, follow: bool, json_out: bool, answer: bool = False
) -> None:
    """Replay from `since`, then stay attached if `--follow`.

    Note what this does NOT contain: reconnect logic. On a real disconnect the user re-runs with
    `--since <last seq>`, and a browser's EventSource does it automatically from `Last-Event-ID`.
    The cursor is the protocol, not the client.
    """
    path = f"/v1/tasks/{task_id}/events"

    # Without --follow the stream must END, or the command hangs on a live socket forever. The
    # server's `follow=false` replays the record and closes.
    params: dict[str, Any] = {"since": since, "follow": str(follow).lower()}
    # A follow stream is a presence; naming its connection lets it tell its own
    # frames from everyone else's. A finite replay is a read, not a presence.
    own_conn = f"cli{secrets.token_hex(8)}"
    if follow:
        params["conn"] = own_conn
    present: set[str] = set()

    for event_name, payload in sse_frames(client.stream_sse(path, params=params)):
        # **The one place the flight recorder talks back.** A `gate.opened` on a stream the user is
        # sitting in front of is a question, not a log line - and a question rendered as a log line
        # is a task that waits 24 hours for an answer nobody realised was wanted.
        #
        # `answer` is set only by `prismoid do --wait`. `prismoid tasks events --follow` is a
        # *recorder*; it must never prompt, because it is what you leave running in a second
        # terminal, and what CI pipes to a file.
        if answer and _asked(payload, task_id, json_out=json_out):
            continue

        if json_out:
            typer.echo(json.dumps(payload))
        elif _news(payload, own_conn, present):
            typer.secho(_render(payload), fg=_COLOURS.get(str(event_name)))

        if follow and ends_follow(event_name, payload):
            sys.stdout.flush()
            return


def sse_frames(lines: Iterable[str]) -> Iterator[tuple[str | None, dict[str, Any]]]:
    """Each `data:` frame of an SSE line stream, decoded, with the last `event:` name seen before it.
    Heartbeat comments and any other line are skipped."""
    event_name: str | None = None
    for line in lines:
        if line.startswith(":"):
            continue  # heartbeat comment - it keeps the proxy awake and the client asleep
        if line.startswith("event: "):
            event_name = line.removeprefix("event: ")
            continue
        if line.startswith("data: "):
            yield event_name, json.loads(line.removeprefix("data: "))


def _asked(payload: dict[str, Any], task_id: str, *, json_out: bool) -> bool:
    """Whether a frame was a gate this terminal prompted for, after prompting.

    A PAUSE gate is nobody's to answer - the task's resume lifts it: it renders as a line like any
    other frame, and the prompt never opens. A CONTROL gate is answered by taking control and
    handing back, never here, except a device hand-off's (`_asked_done`)."""
    if payload.get("type") != "gate.opened":
        return False
    if payload.get("kind") == "control":
        return _asked_done(payload, task_id, json_out=json_out)
    if payload.get("kind") == "pause" or not questions.interactive():
        return False
    _ask_gate(payload, task_id, json_out=json_out)
    return True


def _asked_done(payload: dict[str, Any], task_id: str, *, json_out: bool) -> bool:
    """On a device a hand-off's CONTROL gate is the creator's to end with Done: the browser is a
    window on this computer, and nobody takes it over. (A takeover never happens there: no screen,
    no lease.)"""
    if payload.get("source") != "handoff" or not questions.interactive():
        return False
    device = _device_of(str(payload.get("task_id") or task_id))
    if device is None:
        return False
    questions.ask_done(
        payload["gate_id"],
        title=payload.get("title") or "The agent needs someone at the browser",
        device=device or None,
        as_json=json_out,
    )
    return True


def _ask_gate(payload: dict[str, Any], task_id: str, *, json_out: bool) -> None:
    """A questionless gate (approve / connect) carries no `questions` dict - it renders its title +
    reason and prompts allow / skip, not the numbered batch. A BUDGET gate is neither: its decline
    cancels the task, so it gets resume / cancel and Enter leaves it waiting."""
    if payload.get("kind") == "budget":
        questions.ask_budget(
            payload["gate_id"],
            title=payload.get("title") or "Budget reached",
            reason=payload.get("reason") or "",
            scope=payload.get("budget_scope") or "",
            task_id=str(payload.get("task_id") or task_id),
            as_json=json_out,
        )
    elif payload.get("questions"):
        questions.ask(payload["gate_id"], payload["questions"], as_json=json_out)
    else:
        questions.ask_decision(
            payload["gate_id"],
            title=payload.get("title") or "A decision is needed",
            reason=payload.get("reason") or "",
            scopes=payload.get("scopes") or [],
            host=payload.get("host") or "",
            as_json=json_out,
        )
