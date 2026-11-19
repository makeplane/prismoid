# The task stream — two lanes, one socket (§5.5, §8.6)

A running task emits two kinds of thing, and the frontend needs both without conflating them:

* **Lane 1 — durable `TaskEvent`s.** The record. Every one is journalled, ordered by a monotonic `seq`,
  and replayable. These are the events in [`events.schema.json`](./events.schema.json) — a single
  discriminated union on `type` (24 variants: `task.created`, `step.state`, `artifact.produced`,
  `verification.signed`, `task.delivered`, …). **Only Lane-1 frames carry an SSE `id:`** (the `seq`), so
  `Last-Event-ID` replays exactly the durable record and nothing else.
* **Lane 2 — ephemeral deltas.** The live typing: `message.delta`, `artifact.delta`, `progress`. These
  make the UI feel alive; they are *not* the record, carry **no `id:`**, and are not replayed. Every delta
  stream ends in a durable Lane-1 `final`/snapshot frame — so a client that missed the deltas still gets
  the settled truth.

## Transport

`GET /v1/tasks/{task_id}/events` is the SSE lane (co-primary, §5.5). A WebSocket variant exists for
duplex-preferring clients, but SSE is the contract: `EventSource` gives auto-reconnect and `Last-Event-ID`
for free, which *is* replay built into the transport.

```
event: step.state
id: 42
data: {"type":"step.state","task_id":"…","seq":42,"state":"running", …}

event: message.delta
data: {"type":"message.delta","text":"The Q3 revenue was "}          ← no id: (Lane 2, ephemeral)
```

* **Replay / resume.** Reconnect with `Last-Event-ID: 42` (the browser sends it automatically) or
  `?since=42`; the server resends every durable frame after `seq` 42. `since=0` replays from the start —
  which is how a late joiner catches up on the whole task.
* **No dead air.** While a step runs there is an honest signal at least every **10 s** (§8.6); the first
  live delta arrives within **2 s** of a step starting. A silent stream is a bug, not a quiet task.
* **Streamed text is markdown**, append-only — a client concatenates `message.delta.text` and renders; it
  is never HTML, and a later delta never rewrites an earlier one.
* **Binary artifacts stream *progress facts*, never bytes.** A produced `.xlsx` arrives as an
  `artifact.produced` event with an id to download, not as base64 on the wire.

## Versioning

Every response carries **`X-Prismoid-Protocol: 2`**. The frontend reads it to know which contract it is
talking to; a breaking change bumps it, and a client can refuse a version it does not understand rather
than mis-parse a stream. The two schema files here (`openapi.json`, `events.schema.json`) are exported
from the running app by `just export-contract` — re-run and diff to see any drift.
