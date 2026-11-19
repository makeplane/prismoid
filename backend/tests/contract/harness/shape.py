# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The shape of a turn's durable record: what a behaviour-neutral refactor must not change.

A shape is the turn's `task_events` reduced to structure, and nothing else: which event types, in
which order, and the few fields of each that are structure (a route, a step state, a tool's outcome,
a gate kind). It leaves out everything that varies without the engine's behaviour changing: ids,
timestamps, sequence numbers, text a model wrote, counts of tokens and dollars, byte sizes and
hashes. So a shape fails when the engine does something different and never on wording or timing.

Two pure functions are the whole mechanism, both free of the database and the models:

    shape_of(events)            the record, projected (what a run produced)
    differences(recorded, seen) what does not match, as sentences (what a failure prints)

`KEYS` is the one table that says which fields of which event type are structure. A type the table
does not know is refused rather than guessed: a new event type in a turn's record is a change of
shape that a person should look at, so the harness stops there and names the row to add. An empty
row keeps the type alone.

A step is named by when it first appeared in the record (`s1`, `s2`), never by its id, so two runs
of one scenario draw the same picture. The plan's dependency edges are read from `plan_edges` and
kept under `edges`, by those labels: the planner's `task.planned` carries steps and no edges.

Siblings (two or more steps with the same upstream steps, or with none: a wave) run at once, so their rows
interleave by timing. A wave's rows are recorded grouped by step, in label order, in the places
the wave's rows took (`_by_sibling`): what each sibling did, and where the wave ran, stay structure.

"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from prismoid.schemas.events import DurableEvent

type Shape = dict[str, Any]
type Row = dict[str, Any]

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

# Dotted paths into the event's JSON. A trailing `#` records the length of a list rather than the
# list; a trailing `?` records only that the field is there (an id is never recorded, but a branch
# having a parent is structure). A field that is absent or empty is left out of the row.
KEYS: Mapping[str, tuple[str, ...]] = {
    "task.created": ("mode", "parent_task_id?", "parent_seq?"),
    "task.continued": ("turn", "deliverable"),
    "task.dequeued": ("turn",),
    "task.status": ("status",),
    "task.planned": (),  # the plan itself: `_plan_row`
    "task.delivered": (),  # `artifact_ids` is never filled by the engine, so it is not structure
    "task.renamed": (),
    "step.dequeued": (),
    # `model_ref?`: a fleet seat is pinned to a model; the record says that it is, never which
    "step.state": ("state", "agent", "attempt", "outcome", "blocked_by", "model_ref?"),
    "step.prompt": ("agent", "attempt"),
    "step.narration": (),
    "tool.called": ("tool", "ok", "attempt"),
    "plan.edited": ("edit.kind",),
    "steer.applied": ("safe_point", "superseded#"),
    "steer.rejected": (),  # the reason is a sentence; which steer it was is the scenario's story
    "steer.retracted": (),
    "pause.applied": ("safe_point",),
    "artifact.produced": ("artifact_type", "role"),
    "artifact.revised": ("artifact_type", "revision", "role"),
    "machine.created": ("profile",),
    "machine.rebuilt": ("reason",),
    "machine.woke": ("turn", "profile", "browser_restored", "from_park"),
    "memory.written": ("count", "skipped"),
    "bot_memory.written": ("count", "turn"),
    "context.assembled": (),
    "gate.opened": (
        "kind",
        "questions.questions#",
        "questions.on_timeout",
        "budget_scope",
        "connector_slug",
        "expires_at?",
    ),
    "gate.resolved": ("action",),
    "verification.signed": ("verdict", "suite"),
    "qa.reviewed": ("verdict", "attempt"),  # the long-form lane's editorial read: pass | revise | pass_unread
}

# Events that land when a background model call finishes (the namer starts beside planning and is
# never awaited by it), so their place in the order is timing. They are recorded apart from the
# ordered record, by count.
LOOSE: frozenset[str] = frozenset({"task.renamed"})

# Events whose very presence is timing: warm Machine time is billed as a usage row only when a
# segment lasted a whole second, so a slow run writes one and a fast one none. Left out entirely.
TIMED: frozenset[str] = frozenset({"usage.recorded"})


class Labels:
    """Step ids in order of first appearance: `s1`, `s2`."""

    def __init__(self) -> None:
        self._names: dict[str, str] = {}

    def __call__(self, step_id: object) -> str:
        return self._names.setdefault(str(step_id), f"s{len(self._names) + 1}")


def _at(payload: Mapping[str, Any], path: str) -> object:
    node: object = payload
    for part in path.split("."):
        node = node.get(part) if isinstance(node, Mapping) else None
    return node


def _plan_row(payload: Mapping[str, Any], label: Labels) -> Row:
    """The route, its source and each step's agent and lane part; `route_why` is text and stays out."""
    steps: list[Row] = []
    for step in payload["plan"]["steps"]:
        row: Row = {"id": label(step["id"]), "agent": step["agent"], "kind": step["kind"]}
        if step["lane_role"]:
            row["lane_role"] = step["lane_role"]
        steps.append(row)
    out: Row = {"route": payload["route"], "route_source": payload["route_source"], "steps": steps}
    if payload["machine"]:
        out["machine"] = payload["machine"]
    if payload["fallback"]:
        out["fallback"] = True  # a failed planner's floor ran; why it failed is text
    return out


def row_of(event: DurableEvent, label: Labels) -> Row:
    """One event's contribution to the shape."""
    payload: dict[str, Any] = event.model_dump(mode="json")
    kind = payload["type"]
    if kind not in KEYS:
        raise KeyError(
            f"no shape rule for the event type {kind!r}: add a row to KEYS in tests/contract/harness/shape.py "
            "(an empty row keeps the type alone), or the turn's record changed on purpose"
        )
    row: Row = {"type": kind}
    if payload.get("step_id"):
        row["step"] = label(payload["step_id"])
    if kind == "task.planned":
        row |= _plan_row(payload, label)
    for path in KEYS[kind]:
        key = path.rstrip("#?")
        value = _at(payload, key)
        if path.endswith("#"):
            value = len(cast(list[Any], value)) if isinstance(value, list) else None
        elif path.endswith("?"):
            value = True if value is not None else None
        if value not in (None, ""):
            row[key] = value
    return row


def shape_of(
    name: str,
    outcome: str,
    events: Iterable[DurableEvent],
    facts: Mapping[str, Any] | None = None,
    edges: Iterable[tuple[uuid.UUID, uuid.UUID, str]] = (),
) -> Shape:
    """A run's shape: its ending, the ordered record, the loose events by count, and the plan's
    edges and the moves' facts when there are any."""
    label = Labels()
    ordered: list[Row] = []
    loose: dict[str, int] = {}
    for event in events:
        if event.type in TIMED:
            continue
        row = row_of(event, label)
        if row["type"] in LOOSE:
            loose[row["type"]] = loose.get(row["type"], 0) + 1
        else:
            ordered.append(row)
    labelled = sorted([label(a), label(b), kind] for a, b, kind in edges)
    for wave in _waves(labelled):
        ordered = _by_sibling(ordered, wave)
    shape: Shape = {"scenario": name, "outcome": outcome, "record": ordered, "loose": loose}
    if labelled:
        shape["edges"] = labelled
    if facts:
        shape["facts"] = dict(facts)
    return shape


def _waves(edges: Iterable[Sequence[str]]) -> list[set[str]]:
    """The waves of a plan: two or more steps with the same upstream steps, the first wave's none at all."""
    upstream: dict[str, set[str]] = {}
    sources: set[str] = set()
    for source, target, _ in edges:
        upstream.setdefault(target, set()).add(source)
        sources.add(source)
    waves: dict[frozenset[str], set[str]] = {frozenset(): sources - upstream.keys()}
    for step, sources in upstream.items():
        waves.setdefault(frozenset(sources), set()).add(step)
    return [wave for wave in waves.values() if len(wave) > 1]


def _by_sibling(record: list[Row], wave: set[str]) -> list[Row]:
    """`record` with the wave's rows grouped by step, in label order, in the places the wave's rows took."""
    places = [i for i, row in enumerate(record) if row.get("step") in wave]
    grouped = sorted((record[i] for i in places), key=lambda row: int(row["step"][1:]))
    out = list(record)
    for place, row in zip(places, grouped, strict=True):
        out[place] = row
    return out


# ── comparing ─────────────────────────────────────────────────────────────────────────────────


def differences(
    recorded: Shape, seen: Shape, allowed: Mapping[str, frozenset[str]] | None = None
) -> list[str]:
    """What `seen` does that `recorded` does not, one sentence each; `allowed` maps
    `"<event type>.<field>"` to the values a seen row may carry in place of the recorded one."""
    allowed = allowed or {}
    found: list[str] = []
    for key in ("outcome", "loose", "edges", "facts"):
        if recorded.get(key, {}) != seen.get(key, {}):
            found.append(f"{key}: recorded {recorded.get(key, {})!r}, seen {seen.get(key, {})!r}")
    before, after = recorded["record"], seen["record"]
    if (was := [r["type"] for r in before]) != (now := [r["type"] for r in after]):
        first = next(
            (i for i, (a, b) in enumerate(zip(was, now, strict=False)) if a != b), min(len(was), len(now))
        )
        found.append(
            f"the order of event types changed, first at event {first + 1}:\n"
            f"  recorded: {' '.join(r['type'] for r in before)}\n"
            f"  seen:     {' '.join(r['type'] for r in after)}"
        )
        return found
    for index, (was, now) in enumerate(zip(before, after, strict=True)):
        found += [f"event {index + 1} ({was['type']}): {d}" for d in _row_differences(was, now, allowed)]
    return found


def _row_differences(was: Row, now: Row, allowed: Mapping[str, frozenset[str]]) -> list[str]:
    found: list[str] = []
    for field in sorted(was.keys() | now.keys()):
        old, new = was.get(field), now.get(field)
        if old == new or new in allowed.get(f"{was['type']}.{field}", ()):
            continue
        found.append(f"{field} was {old!r}, now {new!r}")
    return found


# ── the files ─────────────────────────────────────────────────────────────────────────────────

SHAPES = Path(__file__).parents[1] / "shapes"


def read(name: str) -> Shape:
    return json.loads((SHAPES / f"{name}.json").read_text())


def write(shape: Shape) -> Path:
    """One line per recorded event, so a change of shape reads as a change of lines."""
    path = SHAPES / f"{shape['scenario']}.json"
    rows = ",\n".join(f"    {json.dumps(row)}" for row in shape["record"])
    edges = f'  "edges": {json.dumps(shape["edges"])},\n' if shape.get("edges") else ""
    facts = f'  "facts": {json.dumps(shape["facts"])},\n' if shape.get("facts") else ""
    path.write_text(
        "{\n"
        f'  "scenario": {json.dumps(shape["scenario"])},\n'
        f'  "outcome": {json.dumps(shape["outcome"])},\n'
        f'  "loose": {json.dumps(shape["loose"])},\n'
        f"{edges}"
        f"{facts}"
        f'  "record": [\n{rows}\n  ]\n'
        "}\n"
    )
    return path


def names() -> Sequence[str]:
    return sorted(p.stem for p in SHAPES.glob("*.json"))
