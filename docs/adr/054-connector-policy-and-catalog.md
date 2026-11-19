# ADR-054 · Connectors v2: the catalog, one-click OAuth, per-tool policy, and the approval gate

**Status:** accepted · 2026-08-30 · amended 2026-11-14 · amends ADR-020 (its deferral list: the
hosted catalog UI, prompt-based auto-attach, and the needs_reauth notification land here;
credential delegation, stdio transports, RFC 7009 revocation, and per-step scoping stay deferred)

## Context

ADR-020 made a connector real: a user's connection to a remote MCP server, Fernet-sealed
tokens, a server-side OAuth 2.1 walk, explicit per-task attach, and a warden that journals
every call. What it deliberately did not build is everything a person touches: discovery is a
raw URL paste, the OAuth dance needs two separate calls and ends on a dead-end HTML page, the
probed inventory mounts wholesale (every tool the server lists, including `send_email` and
`delete_page`, runs without a pause), and nothing ever suggests a connector, even when the
goal says "email this to me" and a mail-shaped connector sits connected and idle.

The owner's ask, 2026-08-30: a Perplexity-grade connectors surface. A real catalog page,
OAuth that kicks in automatically, per-tool permissions (Off / Always ask / Allow, read-only
vs write groups), and intent-driven attach. Three owner calls recorded from the planning
session: **auto-attach is on by default** (visible and journaled, with write tools gated by
approval as the consent point), **write tools default to Always ask**, and **the catalog
seeds with the dev six** (notion, linear, github, sentry, deepwiki, context7).

## Landscape (compact; primary sources visited during planning)

- **Perplexity connectors**: a curated card catalog; per-tool three-state controls
  (Disable / Always ask / Allow) grouped read-only vs write/delete, with group-level bulk
  set. The permission grammar this ADR adopts, translated into house primitives.
- **Claude (claude.ai / Claude Code)**: connector directories plus per-tool allow/ask
  permission prompts; approvals resolve in place and are remembered per tool. Validates
  ask-at-call-time over ask-at-attach-time.
- **ChatGPT connectors**: catalog of first-party connectors, write actions confirm before
  running. Same posture.
- **MCP ToolAnnotations** (spec 2025-03-26+): `readOnlyHint`, `destructiveHint`,
  `idempotentHint`, `openWorldHint` ride `tools/list`. Adopted as the *default* signal
  (read-only ⇒ allow, else ⇒ ask); annotations are hints from an untrusted server, so they
  pick defaults, never bypass an owner's explicit override.
- **pydantic-ai deferred approvals** (>= 2.9, already pinned): `ApprovalRequired(metadata=…)`
  raised from a toolset hook lands in `DeferredToolRequests.approvals`;
  `DeferredToolResults(approvals={id: ToolApproved()|ToolDenied(…)})` resumes;
  `RunContext.tool_call_approved` marks the re-run. Adopted: this is the library's own
  suspend shape, and HANDOFF §8.3 pre-authorized approvals riding the ask_user spine.
- **Declined**: a learned suggestion router for auto-attach (the deterministic word-boundary
  matcher is auditable and free; a model router here is an unauthenticated control plane
  problem, the ADR-049 lesson); a child `connector_tool_policies` table (see below); remote
  catalog icon URLs in the web (CSP + privacy; vendored monochrome marks instead); UsageEvents
  for MCP calls (no price sheet exists; the approval gate is the side-effect control).

## Decision

**1. The catalog is data, server-side.** `schemas/connector_catalog.py` holds frozen
`CatalogEntry` rows (slug, name, vendor, description, category, server_url, auth
oauth|open, icon, docs_url, keywords, recommended_policies), validated at import: a typo'd
URL is a red test, not a runtime surprise. `GET /v1/connectors/catalog` serves it; the web
renders cards and matches keywords client-side; the CLI resolves names through the API and
`runner/default_connectors.yaml` is deleted (one source of truth). Adding a provider is a
data edit. Seed entries must pass a live probe before landing (`just`-target; several
vendors publish SSE-only endpoints the streamable-HTTP transport cannot speak).

**2. One-click connect.** `POST /v1/connectors/connect` (catalog_slug XOR server_url)
creates the row and, when the probe parks it at `pending_auth`, begins authorization in the
same round trip, returning `{connector, authorize_url|null}`. The callback stops dead-ending:
when `ApiSettings.web_base_url` is set (`COMPUTER_API_WEB_BASE_URL`), the callback 302s to
`{web_base_url}/connectors?connected={slug}&ok={0|1}`; only the server-derived slug and ok
bit ride the redirect (a provider's `error_description` is reflected content and never
enters a Location header). Unset, today's static page stands (CLI-only deployments).

**3. Per-tool policy: a JSONB overrides map, not a child table.** `connectors.tool_policies`
maps tool name → `allow|ask|off`; the *effective* policy is the pure
`effective_policies(overrides, tools)`: override wins, else `default_tool_policy(info)`,
which is ALLOW iff `read_only is True` else ASK (the owner's chosen safe default; the dial
is one function). Annotations are captured at probe time into the `tools` snapshot
(`read_only`, `destructive`). Why not a table: policies are a sibling fact of the cached
inventory on the same row, read exactly where the row is already loaded, written whole from
one PATCH; missing key ⇒ derived default is the natural semantics against a drifting
inventory, where child rows would need garbage collection. A table earns its keep only with
per-policy audit history nobody asked for.

**4. Enforcement, three verbs at two seams.** `off` is inventory subtraction in
`ai/tools/connectors.py::_toolset`: the tool never enters `ConnectorSpec.tool_names`, so the
model never sees it (absent beats broken). `ask` raises `ApprovalRequired` from the
`process_tool_call` hook **before** `warden.run`, so the journal never records a false
`ok=false`; the durable record of the pause is the gate pair. `allow` is today's path.
Ask-state tools are **absent from non-`can_ask_user` agents' inventories** (a scatter map or
gather seat, section_writer, harmonizer, bot_lesson, chair, answerer, selector): parallel seats
can never stampede gates (§14), and no `output_type` widening is needed anywhere, because
`ApprovalRequired` is only reachable on agents whose output union already includes
`DeferredToolRequests`.

**5. The approval gate is `GateKind.APPROVE`, instantiated at last.** One gate per approval
batch (a turn's calls arrive together; one batch per gate is §14's law), `questions=None`,
title "Allow {connector}: {tool}?" or "Allow {n} actions?", `spec.reason` carrying a bounded
`args_preview` (k=v lines, per-value 80 chars, total 500). The preview is user-facing gate
content shown to the owner who must decide; the journal stays arg-keys-only (Rule 9 governs
the flight recorder, not the consent screen). `expires_at = now + 24h`, and **deny-on-timeout
falls out of the existing sweep unchanged**: an approve gate has no questions, so the sweep
reads `on_timeout` as proceed-with-defaults and resolves DECLINE, which the resume maps to
`ToolDenied`. A parked approval therefore can never wedge a task past its TTL, which is what
makes 24h safe (routine-minted tasks included: they get the same gate, an inbox knock, and a
deny at expiry). `gates.resolve` is untouched (accept with `questions=None` already yields
`answers={}`). The park/resume rides the ask_user spine: run ends via deferral, history into
`step.resume_state`, frame kept, step workflow parks on `DBOS.recv`; `store_answer` maps
accept/decline/cancel to an `approval_action`, and the resume builds
`DeferredToolResults(approvals=…)`. Approvals get their own step budget
(`MAX_STEP_APPROVAL_GATES = 8`; the ask budget becomes kind-aware) and the overflow default
is DENY. A mixed turn (ask + approval together) parks on the question gate and denies the
approvals with "retry after the answer": one gate at a time, deterministic; a hybrid gate is
a named follow-up.

**6. Suggestions and auto-attach are deterministic.** `suggest(text, candidates)` is a pure
word-boundary matcher (lowercase, phrases, keywords >= 3 chars). Candidates carry catalog
keywords plus each connector's own tokens: slug, name words, and probed tool-name tokens
split on `_`, which is what makes "email this to me" match any connector exposing
`send_email`, catalog-listed or not. The web runs the same matching client-side for composer
chips (attach on tap; unconnected matches run the one-click flow). Server-side,
`create_task` auto-attaches the owner's LIVE rows whose `auto_attach` flag is on (default
**true**, the owner's recorded call; per-connector opt-out) when the goal matches, only when
no explicit connectors were passed, never on forks or routine-minted tasks, capped by
`MAX_TASK_CONNECTORS`, with provenance journaled as `TaskCreated.connectors_auto`
("slug:keyword" pairs): the attach is visible, owner-configured, and on the record, which is
what keeps it on the right side of the triage doorway law (detectors select machinery, never
grants; the grant here is the owner's standing flag). Named tradeoff: an attached connector
blocks the express lane (`TurnFacts.has_connectors`), converting an express-eligible ask
into a planned task. Correct when matched (express mounts no tools and could not have used
the service), and the word-boundary matcher keeps false conversions rare.

**7. The rest of the surface.** `disable`/`enable` endpoints finally write
`ConnectorStatus.DISABLED` (it was rendered everywhere and written nowhere).
`InboxKind.GATE_OPENED` comes alive for approval gates, and a new
`InboxKind.CONNECTOR_BROKEN` knocks on the needs_reauth transition edges (dedup: only when
the previous status differed), closing ADR-020's deferred notification. `GateOpened` and
`GateOut` gain an additive `reason` field so budget and approval gates carry their story on
the wire. The web gets `/connectors` (the first settings-shaped route: catalog cards,
Connected/Available filter, peek detail with the permission rows), the settings panel slims
to a quick roster plus a pointer, the composer gains the suggestion chip, the task page gains
the ApprovalDock, and the progress tree learns connector verbs.

## Consequences

- Annotation-less servers (most, today) default every non-read-only tool to ASK. That is the
  chosen posture: the first send_email pauses once, the owner flips the tool to Allow if they
  want silence. Said loudly in the release note.
- A parked approval holds its frame container up to 24h (the ask park's existing trade,
  now more frequent). Deny-on-timeout bounds it; destroy-on-park with snapshot rebuild is the
  named follow-up if container pressure shows up.
- MCP calls still emit no UsageEvents: the approval gate is the only side-effect control, and
  is framed as such (a side-effect guard, never a money guard).
- `resolve_for_task` already live-probes per step; policy derivation adds no network.

## Amendment (same day, 2026-08-30): the connect block, the grown directory, the modal

The `propose_connector` deferral came off the deferred list within hours - the owner's call
("show a connection block there in the conversation... connect proactively... then it
continues on to the progress"), and it landed as **the connect gate**:

- **`request_connector(service, why)`** - a second deferred tool beside `ask_user`, same
  `can_ask_user` grant (both need the widened output type; both belong to top-level step
  agents). Amended 2026-10-04: it is its own declared grant, `connector.request`, held only by
  agents that can ask, and the step prompt's connect block follows that grant. The navigator
  asks for a login but never holds it, since its door already ruled a connector out; a bot that
  may ask keeps it. The prompt teaching is honest and sharp: a goal that NAMES a service is asking to
  use it - answering from memory instead is not doing the task (the first live run proved the
  soft wording insufficient; the writer answered a "use deepwiki" ask from its own weights).
- **`GateKind.CONNECT`** - the fourth gate kind, instantiated by `gates.open_connect`:
  `questions=None` (deny-on-timeout structural, the approve gate's law), `connector_slug` on
  the spec/event/wire naming what the block offers (a catalog entry, the creator's own
  not-yet-attached row, or honestly nothing), `reason` = the agent's one sentence, its own
  step budget (2, overflow answers itself), an inbox knock. **`resolve` on ACCEPT attaches
  the creator's now-live connector to the task before the wake** - which is what makes "and
  then it continues on to the progress" literal: the resumed attempt re-resolves
  `task.connector_ids` and mounts the fresh tools. The receipt (`{"connected": bool}`) rides
  the resolution content, and the tool's resume result tells the model exactly what happened
  (connected-and-attached · accepted-but-unattached · declined). Mixed turns stay one-gate:
  questions outrank connects outrank approvals; every other pending id resolves with a
  re-request-later string.
- **The web block** (`components/tasks/connect-dock.tsx`) - the service card in the
  conversation: brand mark, the agent's why, one Connect button running the SAME one-click
  flow the page uses, and the point of it all: **the block resolves itself the moment the
  connector goes live** (accept → attach → resume). Skip declines; the inbox card wears the
  same body plus Cancel task. **Its own visual species** (owner call, same day): a
  CONNECTION NEEDED kicker, the directory's size-12 brand well, the service as headline, one
  primary Connect action, continuing-without as a quiet text link - deliberately none of the
  Q&A docks' grammar (no option rows, no verb pairs, no stepper), so a connection request
  can never be misread as another question. The clarifier is taught never to ask about credentials or
  connectedness - the block is the better surface (its "Do you have a Deepwiki login?"
  question on the first live run is why).
- **The directory grew 6 → 38**, every URL probed live on landing day (33 candidates tested,
  32 real; `shopify.dev/mcp` failed and did not ship): the seven official Google Workspace
  servers (Gmail's 23 tools included), Stripe/PayPal/Square, Atlassian/Asana/monday/Todoist,
  HubSpot/Intercom, Canva/Figma/Jam, Webflow/Wix/Netlify/Vercel, Neon/Supabase, two
  Cloudflares, Semgrep/Globalping, Box/Fireflies/Hugging Face. The probe drill learned the
  GOOGLE SHAPE (lists tools anonymously, locks the calls): an oauth entry answering an
  anonymous probe is a note, never a failure - the hard failure stays `open`-declared servers
  that demand auth. `CatalogEntry.auth_note` says a dance's catch up front (Google ships no
  dynamic client registration - you bring an OAuth client), and 24 more Simple Icons marks
  vendored (31 total).
- **The detail is a MODAL, centered** (owner call: "a modal form", the drawer read as a
  slider) - and a directory card is clickable BEFORE connecting: catalog-only refs open the
  entry face (description, vendor, auth note, docs, Connect) of the same dialog.

## Second amendment (2026-08-30, evening): the dial flips to ALLOW; verb tokens stoplisted

Living with the ask-default for one afternoon settled it (owner: "I don't need tools accept
on the dialog... tools can automatically be picked up automatically"): an unannotated server
made every READ a dialog, and serial approvals after an explicit Connect click re-asked a
question the user had just answered. **`default_tool_policy` now returns ALLOW for
everything** - connecting is the consent. The machinery is untouched: the permissions screen
still sets any tool to `ask` or `off` with one PATCH, the approval gate still fires wherever
`ask` is set, fan-out seats still shed ask-tools, and the dial remains one pure function if
the posture ever moves again. This supersedes the planning-session "write tools ask" call,
by the same owner, with the live feel in hand.

Same evening, found live: "Create a Notion page..." auto-attached Gmail - `create_draft`'s
first token made the bare verb "create" attach a credential. **`GENERIC_TOOL_TOKENS`**: bare
CRUD-verb tokens (create/get/list/search/send/...) never enter the auto-attach or suggestion
vocabulary; multi-token phrases ("send email", "create draft") and nouns ("email", "draft")
stay - a phrase is intent, a bare verb is the language. One stoplist, mirrored in the web
matcher, regression-pinned on both sides.

And the end-to-end OAuth leg was proven against the real Notion: one click in the
in-conversation block minted a genuine consent URL (live dynamic client registration against
mcp.notion.com, PKCE, state) and Notion's login/consent page rendered; the login itself is
the user's act, and every leg after it is covered by the mock-AS integration walk and the
open-server completions.

## Third amendment (2026-08-30, night): the action lane - the operator, the door, the receipt

Found live, the sharpest failure of the arc: "Create a Notion page titled Connector Test
with a two-line hello note in it" - Notion connected, mounted, named - delivered a DOCUMENT
titled "Instructions to Create the Notion Page". Nothing misbehaved: the planner's whole
vocabulary culminated in publishing an artifact, so it turned an action into homework for
the user. The gap was orchestration, not the agent.

**The action lane**: an imperative aimed at a service the task can reach is a task whose
deliverable is the SIDE EFFECT plus a receipt - nothing is published.

- **The door** (`ai/policy/action.py`, deterministic like every policy decision): fires only
  when BOTH halves are unambiguous - the message LEADS with one of a closed set of action
  verbs (courtesy prefixes stripped; growth is a data edit, never a synonym guess) AND a
  reachable service is named by the ONE suggestion grammar (`schemas/connector_catalog.
  suggest`, the same word-boundary matcher the composer chip and auto-attach speak - three
  surfaces that can never disagree about what counts as naming a service). Bounded at 500
  chars: a long brief belongs to the planner. Candidates are attached connectors first (they
  outrank), then the owner's other LIVE rows (via the extracted
  `operations/connectors.service_words`), then catalog entries - so a named-but-unconnected
  service still takes the lane, and the operator's first move at run time is the connect
  block.
- **The operator** (`ai/agents/operator.py`, FAST_EXECUTOR, `can_ask_user=True`,
  `output_ref=None`): performs the action through the mounted connector tools and narrates a
  1-2 sentence receipt naming what the service reports (titles, ids, links). Its one law:
  the deliverable is the side effect, never a document about it. It raises
  `request_connector` when the service is missing and asks rather than guesses.
- **The plan** (`action_plan`): ONE step, `contract.output=None` - the express lane's
  zero-artifact delivery carries the receipt as the answer; `verify=False` because the
  flight recorder's `tool.called` rows ARE the evidence and there is no artifact to
  re-derive. Routed record: `route="action"`, source `detector`, why wearing verb + keyword.
- **The seat in the chain** (`draft_plan`): after the dials (an explicit user Document dial
  still wins - dial > detector) and scatter, before express and the planner - guarded to
  standard mode and document/unresolved shapes.
- The mounted-connectors briefing (`ai/context/brief.py`) now says it to every agent: when
  the goal is an ACTION on a mounted service, PERFORM it - never deliver instructions.

Proven live the same night, the exact failing prompt re-run: auto-attach `notion:notion`,
`route=action` ("an act-on-Notion ask (create + 'notion')"), one operator step,
`notion_notion-create-pages ok=true` in 540ms, delivered in 5.7s at $0.0151 with the receipt
as the answer: Created Notion page "Connector Test" (the service's own URL). The first run
of this prompt took minutes and produced a five-page manual.

## Fourth amendment (2026-11-14): the dial stays; server text loses invisible characters

The second amendment's dial stands: tools on a connector you connected yourself run, and on an
intent-attached or bot-manifest connector a tool not labelled read-only asks. Server text the
model reads (descriptions, the text inside input schemas, instructions) and the approval card's
args preview lose every invisible character (Unicode Cc and Cf, the tag block) and are capped,
at the probe and again at the mount.

Once a run has read third-party text (a page, a search, the browser, another service), a tool not
labelled read-only asks first, even on your own connector; a step that cannot ask is refused the
call. Only when someone can answer: an unattended run (a routine's run, its turn 1;
`TaskDeps.unattended`) runs the write as the dial says, and an explicit `ask` still asks
(owner, 2026-11-14).

**Amended 2026-11-14: who holds a task's connectors is a grant.** `ai/agents/grants.CONNECTORS` names
the agents that mount a task's connector tools (the answerer the reads alone; a bot all of them, as
before). A fan-out seat (a scatter map or gather seat, section writer, harmonizer, chair) and a
single-call agent hold none: a seat reads untrusted pages and needs no private data.

## Fifth amendment (2026-11-16): an approval shows what will run, and only to its creator

A security review of the code found three gaps around the approval. The first, a stranger's page
or mail steering a task into the person's own accounts, is closed by the fourth amendment: once a
run has read outside text, a tool not labelled read-only asks first. The other two are closed here.

- **The card shows what will run.** Accepting runs the whole call, yet the args preview cut
  each value at 80 characters and the whole at 500, so a harmless first line could hide the rest
  of an email body. A value now shows up to 2,000 characters and the preview up to 8,000, and a
  cut says how many characters it left out.
- **Only the creator reads the arguments.** They run on the creator's credential (an email's
  recipients and body), yet every project member read them on the gates API and the task stream.
  `schemas/gate_view.viewer_reason` gives everyone else each held call's `connector: tool` line
  and nothing more, where a gate leaves the server (`api/serializers/gates.py`,
  `events.redact_for`); a device-away gate's command stays the creator's, as before.

## Deferred (named follow-ups)

Hybrid mixed ask+approval gate · per-call verdicts inside a batch (`GateResolveIn.content`
leaves room) · approval carry-over across ladder retries (a wall-clock retry re-asks today) ·
destroy-on-park frames · UsageEvents for MCP calls · bots mounting `manifest.connectors` ·
per-step connector scoping · space-shared connectors · RFC 7009 · stdio transports · remote
catalog icons.
