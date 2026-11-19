# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Read SSE frames on stdin, parse them through the event union, print what a client would render.

The stream is meant to be legible in a terminal — `curl -N "$B/v1/tasks/$ID/events"` is the
canonical first look at any task. This is the other half of that promise: proof that the
bytes on the wire parse into typed events, and that the id rule holds (only durable frames
carry `id:`).

    curl -N -H "Authorization: Bearer $T" "$B/v1/tasks/$ID/events?since=0" | uv run python tools/parse_frame.py
"""

from __future__ import annotations

import sys

from prismoid.schemas.events import TaskEventAdapter, is_durable


def main() -> int:
    raw = sys.stdin.read()
    frames = [f for f in raw.split("\n\n") if f.strip()]
    if not frames:
        print("no frames on stdin", file=sys.stderr)
        return 1

    for frame in frames:
        name: str | None = None
        sse_id: str | None = None
        data: str | None = None
        for line in frame.splitlines():
            if line.startswith(":"):
                print(f"  heartbeat  {line}")
            elif line.startswith("event: "):
                name = line.removeprefix("event: ")
            elif line.startswith("id: "):
                sse_id = line.removeprefix("id: ")
            elif line.startswith("data: "):
                data = line.removeprefix("data: ")
        if data is None:
            continue

        event = TaskEventAdapter.validate_json(data)
        dumped = event.model_dump()
        durable = is_durable(event)

        print(f"  event:     {name}")
        print(f"  parsed as: {type(event).__name__}")
        print(f"  lane:      {dumped['lane']}")
        print(f"  id:        {sse_id!r}   (durable={durable})")
        print(f"  task_id:   {dumped['task_id']}")
        if durable:
            print(f"  seq:       {dumped['seq']}")

        # The id rule, asserted on real bytes rather than trusted: a Lane-2 delta that carried an
        # `id:` would let a reconnect skip durable events, so the rule is checked, not documented.
        assert (sse_id is not None) == durable, "the id: rule is broken (§5.5)"
        assert name == dumped["type"], "the SSE event name must be the union discriminator"
        print("  ✓ id-rule holds: only durable frames advance Last-Event-ID")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
