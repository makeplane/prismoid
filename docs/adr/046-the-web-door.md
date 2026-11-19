# ADR-046 · The web door: `web_fetch`, the egress guard, and the document sidecar

**Status:** accepted · 2026-08-24

## Context

A user pastes a URL and the system is helpless: the text lane (ADR-019 — chat answers, DM
bots) has no frame and therefore no network at all, and on the artifact lane the only web is
`curl` through the frame's egress proxy — raw HTML into a frontier model, working only when
One predicted the right `allow_domains` at plan time, at the price of a frame boot to read a
page. HANDOFF §1's doctrine line — *"Sandbox + read-only web only"* — has always permitted
reading the web; nothing implemented it as a capability agents actually hold.

The landscape pass preceding this ADR (an unpublished research note of 2026-08-24) is the
standards record: what the platform vendors ship, what the extraction
field looks like, which packages are load-bearing across the industry, and the adopt/decline
table. This ADR records the decisions; the research note carried the receipts.

## Decision

**A tool, not a skill, and not a new sandbox.** A skill is judgment in markdown — it cannot
mint capability. `web.fetch` is a granted tool in the **worker process**: the third
instance of the settled posture after connectors (ADR-020) and the media lane (ADR-024) —
explicit grant, keys-only journal, frame egress bypassed *by design* because the frame's
no-network law is about model-written code, and this is a system-owned pipeline the model may
only ask questions of. The module is `ai/tools/fetch.py`; the model-visible tool is
`web_fetch`; the journal name is `web.fetch`; the grant rides `AgentDef.tools` and
`BOT_TOOLS` (a bot author may grant it — read-only public web, the same hands a built-in
holds). Granted to the `researcher` and the `writer` at birth: the writer is what answers the
text lane, and before this door a pasted link there was a dead end.

**The guard fetches; nothing else ever dials.** The parse-time door moved from
`schemas/connectors.py` to **`schemas/egress.py`** (shared home, re-exported — one copy of
each rule): every literal spelling of loopback/private/link-local/metadata refused, plus the
web door's own temper (`vet_fetch_url`: http allowed but upgraded to https first with an
honest fallback note; credentials in the netloc refused; fragments stripped). Then the depth
ADR-020 recorded as a deferral becomes mandatory here, because a *model-authored* URL is the
DNS-rebinding threat model itself: **resolve-and-pin** — every DNS answer classified by
`refuse_ip` (one private A record poisons the whole answer), the connection made to the
vetted address itself with the hostname riding the Host header and the TLS SNI
(`sni_hostname`, so certificate verification still binds to the name), and **every redirect
hop re-vetted and re-resolved** under our own redirect loop (`follow_redirects=False`,
`trust_env=False`). Connectors keep their parse-time-only stance — a handful of
user-configured servers is a different threat model, and that deferral stays recorded there.

**trafilatura reads pages.** `extract(output_format="markdown", with_metadata=True,
include_links=True, include_tables=True)` with its own documented ladder (default →
`favor_recall` → `html2txt`), run under `asyncio.to_thread`. Chosen as the industry-standard
extractor — RefinedWeb, FineWeb, FineWeb2, NeMo Curator, IBM data-prep-kit; best single tool
in SIGIR 2023 — **Apache-2.0 since v1.8.0**, pinned `>=2.2` (pre-1.8 is GPLv3 and never
enters this tree). Its caveats are handled at our layer: the built-in `EXTRACTION_TIMEOUT` is
signal-based and CLI-only, so our wall clocks govern; JS-only pages fail honestly with the
browse lane named as the future escalation.

**markitdown normalizes files — behind a process boundary, and it never fetches.** MIT,
Microsoft, the format-breadth standard (DOCX via mammoth is genuinely good; PDF is a
recorded ceiling — docling is the named upgrade when table-faithful PDF reading is asked
for). Two homes: (1) the fetch tool's binary branch — PDF/DOCX/PPTX/XLSX/EPUB bytes to
markdown via **`ai/tools/convert.py`**, a sidecar subprocess that sets its own rlimits
(address space, CPU seconds, file size) *before* importing markitdown and is killed on the
parent's wall clock — because fetched binaries are untrusted input and markitdown's parsers
have recorded sharp edges (an open XXE report in the DOCX math path, no decompression caps
on ZIP recursion); (2) the frame image, pinned, so `markitdown /task/inputs/x.docx` replaces
the hand-rolled python-docx script per read — in-frame the sandbox itself is the boundary.
Format extras only (`[docx,pptx,xlsx,pdf]`), never `[all]` (Azure SDKs, an audio lane whose
default recognizer is a network call). Its `convert_uri` lane and the `markitdown-mcp`
package are **refused outright** — both have live SSRF findings; in Plane, bytes go in,
markdown comes out, and only the guard dials.

**The context anchor (anti-exfiltration).** `web_fetch` serves only URLs already present in
the run's own context — the goal, the thread, an input, or a page previously fetched (links
you read are links you may follow). A URL produced from the model's weights is refused with
the fix in the message (ask the user). This is the stance the platform vendors converged on
(Anthropic's server-side web_fetch enforces exactly this), and it closes the classic
injected-page exfiltration shape: a page cannot direct the agent to carry task content to an
attacker's collector, because the collector's address was never in the task.
`COMPUTER_WORKER_FETCH_CONTEXT_ANCHOR=false` is the recorded dial for self-hosters who accept
free-range fetching.

**Tightened 2026-08-24 (review), in two places the first cut was looser than it read.** (a) The
host test was a SUBSTRING test, so `"example.com" in "see notexample.com"` anchored a host
nobody named; it now parses the hostnames context actually mentions and compares on a label
boundary, subdomains included. (b) Host-level anchoring alone left the QUERY STRING free — an
attacker's page mentioning a host it controls bought `?leak=<the task's secrets>` on that host,
which is the exfiltration shape the anchor exists to close, wearing the anchor's own checkmark.
So the rule is now three, narrowest first: the exact URL in context is always fine (which is how
a link read off a fetched page stays followable, query and all — trafilatura keeps links); a
query string the model COMPOSED is refused even on a named host; otherwise the host must be one
context names, or a subdomain of one. The two refusals carry different messages, because a model
told "not mentioned anywhere" about a host that plainly is mentioned argues with the tool instead
of correcting itself. And the fail-open branch — no visible context, anchor inert, kept so a
pydantic-ai shape change can never brick the tool — now says so on the flight recorder, because a
security control that switches itself off in silence is one nobody discovers. One nuance, found live (2026-08-24, the python.org run): **the planner is
a trusted author of context** — a URL One writes into a step's goal ("fetch an authoritative
source, e.g. python.org") anchors the fetch exactly as a user's mention would. That is the
intended semantics: the planner reads only user-authored material, so injection cannot reach
it — with one recorded edge: a follow-up turn's thread carries previously *fetched* text into
the planner's view, so a poisoned page could in principle steer a later plan's URLs. Accepted
for now (the fetch itself stays guarded + journaled); revisit if follow-up threads ever mount
third-party content wholesale.

**Tightened again 2026-11-04 (security review).** A composed PATH or SUBDOMAIN was the
query-string hole in other clothes: an attacker's page puts its own host in context, and
`https://attacker.example/c/<secret>` or `https://<secret>.attacker.example/` passed. The rule is
now two: the exact URL in context is always fine; otherwise only the bare origin (`https://host`
or `https://host/`, no path, query or port) of a host context names EXACTLY (`www.` aside). A
composed path gets its own refusal pointing at the front page. The model's own replies no longer
count as context: a URL it wrote is a URL it composed. And the host scan is a linear tokenizer:
the old label regex backtracked quadratically on a crafted `a-a-a-...` run.

**Amended 2026-11-14: the anchor reads what the person wrote, and fails closed.** Every tool return
counted as context, so a page's TEXT anchored any URL it printed (with whatever query it chose), and
an empty history switched the anchor off. Now the anchor reads only the user prompt parts (the goal,
the thread, a steer), the person's `ask_user` answers, a `read_file` of the read-only `/task/inputs`
mount (their attachments, and a parent turn's artifacts, which were already delivered to them) and
`web_search` results (the search query is already that tool's own exit, ADR-047); a fetched page, a
file the agent wrote or a connector reply anchors nothing.
A link a page carries stays followable through the tool's own side channel: every `<a href>` on a
page `web_fetch` read, made absolute and vetted, may be fetched by exact match within that toolset.
An empty history refuses like any unanchored URL. The fail-open branch and its warning are gone.

**Refusals are results; everything is journaled.** Every call — success, failure, denial —
lands as `tool.called` with summary = host + clamped path, **never the query string** (query
params are where tokens and exfiltration ride; Rule 9's keys-not-values, applied to URLs). That
law covers `denied_reason` too, which the first cut missed (fixed 2026-08-24, review): an
exception's own prose carries the URL it failed on, so `web_fetch`'s 404 wrote the token back
onto the same append-only row that `fetch_label` had just cleaned — and the web renders the
reason verbatim. The scrub now lives in `call_rows.journal_row`, one place (amended 2026-11-14: the warden is gone), covering every tool that
will ever raise; the MODEL still receives the whole URL in the tool's result, because it needs
it to re-ask.
A refusal returns as the tool's *result* with the fix in the message (the `read_file`
missing-file lesson), never a `ModelRetry` that burns the schema budget — but it is journaled
`ok=false` first, so the flight recorder shows every denial. Long documents paginate
(`start_index`) from a per-step cache; page content arrives fenced as *data, not
instructions* with the fence stating the char range honestly. Caps are conf
(`fetch_timeout_s` 30 · `fetch_max_bytes` 20 MB · `fetch_max_redirects` 5 ·
`fetch_max_per_step` 12 · `fetch_page_chars` 40k · `fetch_convert_timeout_s` 60 · the anchor
· a dev-only loopback flag, the connectors twin).

**Robots stance, recorded:** single user-directed fetches on a user's behalf, an honest
User-Agent naming us, no crawling, no link-fanning. robots.txt governs crawlers; a person
asking their agent to read one page is the person reading the page. Crawling would be a
different feature and a different ADR.

**Declined, with reasons** (the research doc carries the evidence): provider-native web
tools (Anthropic/OpenAI/Gemini — welded to their own APIs; §9 model-agnosticism means every
model gets the same hands); hosted readers (Jina/Firecrawl — user URLs leaving the product,
the E2B-for-sandbar rejection re-applied; Firecrawl's self-host core is AGPL with its
differentiators cloud-only); pymupdf4llm and marker (AGPL / GPL-plus-restricted-weights);
html2text (GPL); a fetch skill without a tool (capability cannot be prompted into
existence).

## Consequences

The doctrine line survives untouched — this *is* "read-only web". Zero migrations, zero new
event kinds: `tool.called` already renders in the rail with `tool_call_id` reconciliation.
Deploy order is ordinary (worker restart picks up the venv; the frame image rebuilds on next
build — `_image_tag` hashes the Dockerfile change). Two §3 rows: `trafilatura>=2.2` and
`markitdown[docx,pptx,xlsx,pdf]>=0.1.7,<0.2` (upstream itself treats 0.2 as a break; both
exact-pinned by `uv.lock`, and the frame image pins explicitly).

**The footprint, stated honestly** (corrected 2026-08-24, review — the first draft said "the
worker venv gains onnxruntime", which described a venv this repo does not have). Both rows are
base `dependencies`, and `backend/Dockerfile` builds **one image for all three roles** — api,
worker, migrator all run `${BACKEND_IMAGE}` in `compose.prod.yaml`, deliberately: "one artifact
to build, sign, and promote, not two that can drift." So the transitive tail lands in every
role, the internet-facing API included: **≈257 MB of site-packages** — onnxruntime 112 (magika,
markitdown's type sniffer), pandas 49, sympy 29, numpy 25, lxml 20, pillow 14, plus magika,
pdfplumber, pypdfium2, trafilatura and markitdown themselves. That is the real price of the
file door, and it is paid by a process that never fetches and never converts.

Accepted rather than split, because the one-image rule is older and load-bearing and this ADR
is not the place to overturn it. Recorded as the standing reason to revisit: if the API image's
size or CVE surface ever becomes the problem, the fix is an `[project.optional-dependencies]`
extra plus a second build target — a day's work, and the dependency graph already names exactly
which rows would move.

**Amended 2026-11-12: a page the web door read counts as read for `links:resolve`.** The document
suite's link check held every link to two lists: the declared sources and the producing step's
egress allowlist, on the reasoning that a frame has no route out but its proxy. This door never
crosses that proxy (it runs in the worker, by design), and a browser host a person let through
widens the Machine, not the step's contract, so a correct, web-sourced memo signed FAIL ("0 of 8
resolve": one writer, 13 fetches, every link a page it had read). The verifier now reads the
task's journal too: every `ok=true` `web.fetch`, and every page the browser loaded (an `ok`
`browser.open` or `browser.snapshot`, and the page an `ok` click, type, fill, select or press acted
on: `labels.READ_TOOLS`), from any step and any turn of the task, reach the suite as `read_pages`,
host+path as the label spells them (`labels.page_read`). A link passes when its host+path is one of
them: host lowercased, scheme, query and fragment ignored, a trailing slash ignored, and a label the
`LABEL_MAX` clamp cut matched by prefix. A fetched host with an unread path still fails, a failed
fetch read nothing, another task's reads are not this one's, and a URL a search only returned is
no read (search results are not journaled, and a snippet is not the page). The allowlist rule
stands beside it unchanged. Still uncounted: a page reached by a click and read only in the
snapshot that came back with it, until something on it is acted on or snapshotted, since the
journal names the page acted on, not the page landed on.

**Amended 2026-11-14: `web_fetch` reads for a goal, and one extractor names every site.** The tool
takes `goal` and `view`. `extract` (the default) hands the page and the goal to the page reader, a
`cheap-classifier` agent built through `build()` like every other (so the meter rides each call), and
returns only its quotes and a short summary; a long page is read in 60k-character chunks together.
Code checks the reader's work: a quote that is not in the page, word for word once spacing and quote
marks are evened out, is dropped and the drop counted on the result, so the agent never reads a quote
the page does not contain (`ai/tools/reading.py`). `table` returns the page's tables; `raw` is the
slices this ADR first shipped. With no goal, no reader wired, or a reader that fails, the call shows
the page as read and says why. The anchor, the refusals journaled as results and the host+path
summaries are unchanged. Every site a text names, a link or a bare host, is read by one extractor,
`schemas/egress.sites_in`, with its 254-character ReDoS bound: the anchor, the computer door
(`ai/policy/navigate.py`), the express triage's link test and the record's reason scrub all use it,
and their own host and URL patterns are gone.

Named and deferred: **`web.search`** is ADR-047's seam (engine-as-config behind our own
tool, the Bing-retirement lesson; the media lane's `UsageEvent` pattern with a `SEARCH`
unit); the **browse lane** (playwright + crawl4ai in a frame-image variant) waits for a real
task that needs JavaScript; **docling** waits for a task that needs table-faithful PDF;
**video and audio reading** are named lanes for the uploads door (vision seats already read
images — a fetched image returns as pixels today). The `web-research` skill rides the skills
loop, not this ADR.

**Amended 2026-11-15 (ADR-092 §8, the v1 doors deleted):** The navigate door and the computer floor are deleted (`ai/policy/navigate`); the hosts a plan's browser step may open without asking are still the ones the person typed (`schemas/computer_use.named_sites`), and a connector-covered host is left to its connector.
