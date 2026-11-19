# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The minimal stream page.

A browser rendering of the live task stream - the same SSE lane the CLI's flight recorder reads,
seen by a person instead of a terminal. It is deliberately one self-contained file: no build step, no
framework, no bundle. It exists to *prove the stream is watchable*, which is a different claim from
"the stream exists", and the only way to make it is to open it in a browser and watch a task run.

**The cursor is the whole protocol.** Only durable frames set `id:`, so the newest id the page has
seen is all it needs to resume: a dropped stream is reopened with a fresh single-use ticket and
`?since=<that id>`, and the server replays Lane-1 frames from there. (The browser's own retry would
carry the spent ticket, so the page closes the source and reopens it itself.)
"""

from __future__ import annotations

from uuid import UUID

from fastapi.responses import HTMLResponse

# The token is read from the URL fragment (`#token=…`), never a query param: a fragment is not sent
# to the server, does not land in access logs, and does not leak through a Referer header.
# The page spends it only in an Authorization header, minting a single-use stream ticket; the
# ticket, never the token, rides the EventSource URL.
_PAGE = """<!doctype html>
<meta charset="utf-8">
<title>task {task_id}</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 14px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; margin: 0; padding: 1.5rem;
         max-width: 60rem; }
  h1 { font-size: 1rem; font-weight: 600; margin: 0 0 1rem; }
  h1 .id { opacity: .5; font-weight: 400; }
  .status { display: inline-block; padding: .1rem .5rem; border-radius: .3rem; background: #8883; }
  #log { display: grid; grid-template-columns: max-content max-content 1fr; gap: .1rem .8rem; }
  .seq { opacity: .4; text-align: right; }
  .type { font-weight: 600; }
  .delta { white-space: pre-wrap; opacity: .85; grid-column: 3; }
  .done .type { color: #2a2; } .failed .type { color: #c33; } .escalated .type { color: #e80; }
</style>
<h1>watching <span class="id">{task_id}</span> · <span class="status" id="status">connecting…</span></h1>
<div id="log"></div>
<script>
const taskId = {task_id_json};
const token = new URLSearchParams(location.hash.slice(1)).get("token") || "";
const log = document.getElementById("log");
const statusEl = document.getElementById("status");

// since=0 replays the whole history, then the same stream goes live. A ticket opens one stream
// once, so a drop is reopened here with a fresh ticket from the newest durable id seen.
let since = 0;
async function mintTicket() {
  try {
    const r = await fetch(`/v1/tasks/${taskId}/events/ticket`, {
      method: "POST", headers: { Authorization: `Bearer ${token}` } });
    return r.ok ? (await r.json()).ticket : "";
  } catch { return ""; }
}
async function connect() {
  const ticket = await mintTicket();
  if (!ticket) { statusEl.textContent = "reconnecting…"; setTimeout(connect, 5000); return; }
  const src = new EventSource(`/v1/tasks/${taskId}/events?since=${since}&ticket=${encodeURIComponent(ticket)}`);
  src.onopen = () => statusEl.textContent = "live";
  src.onerror = () => { statusEl.textContent = "reconnecting…"; src.close(); setTimeout(connect, 2000); };
  src.onmessage = onFrame;
}
connect();

function onFrame(e) {
  if (Number(e.lastEventId) > since) since = Number(e.lastEventId);
  let f; try { f = JSON.parse(e.data); } catch { return; }
  if (f.type === "task.status") statusEl.textContent = f.status;

  // A delta is a preview of a document being written: append to the same row rather than stacking,
  // so the reader watches it grow instead of scrolling a thousand fragments (§8.6, append-only).
  if (f.type === "artifact.delta") {
    let row = document.getElementById("delta-" + f.channel);
    if (!row) { row = document.createElement("div"); row.id = "delta-" + f.channel;
                row.className = "delta"; log.append(row); }
    row.textContent = f.text || "";
    return;
  }

  const seq = document.createElement("div"); seq.className = "seq"; seq.textContent = f.seq ?? "";
  const type = document.createElement("div"); type.className = "type"; type.textContent = f.type;
  const detail = document.createElement("div");
  detail.textContent = f.state || f.status || f.title || f.summary || f.reason || "";
  const state = f.state || (f.type || "").split(".").pop();
  for (const el of [seq, type, detail]) el.classList.add(state);
  log.append(seq, type, detail);
}
</script>
"""


async def watch_page(task_id: UUID) -> HTMLResponse:
    """The page itself. Auth is the stream's (a ticket minted with the token in the fragment); this
    HTML is inert - it carries no data, only the code that fetches it over the authenticated stream."""
    import json

    body = _PAGE.replace("{task_id}", str(task_id)).replace("{task_id_json}", json.dumps(str(task_id)))
    return HTMLResponse(body)
