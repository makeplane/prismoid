# ADR-047 · The search lane: `web_search`, engines as config, queries on the ledger

**Status:** accepted · 2026-08-24

## Context

ADR-046 opened the web door: an agent reads any URL the task names. The sibling gap is
discovery — *finding* the URL. The platform tool-roster research
(the unpublished research note of 2026-08-24 behind ADR-046) established that
search + fetch are the two table-stakes rows Plane was missing, that every vendor's native
search tool is welded to its own API and billing (Anthropic $10/1k · OpenAI $10/1k · Gemini
$14/1k — unusable under §9 model-agnosticism), and that the raw-SERP market is a
dependency-risk lesson in itself: **Microsoft retired the Bing Search APIs on 2025-08-11 with
~3 months' notice**, and Google's Custom Search JSON API is closed to new customers. Any
single engine can vanish on a quarter's notice.

## Decision

**A tool, with engines as CONFIG — never code, held all the way down.** `ai/tools/providers/
search.py` is the second instance of the media-lane doctrine (`providers.py`: a provider that
isn't a chat model is a tool): one file speaks every engine's wire API, the warden grants it
(`web.search`; the model sees `web_search`), and `build()` is **generic by construction — it
knows no engine's name**. Each wire registers a self-describing `WireSpec` (needs a key? an
operator URL? a model? may it ride a worker provider key?), and everything an engine needs
arrives through **four flat `name=value` grammars** (the throttle precedent):
`COMPUTER_SEARCH_ENGINES` (the ordered chain) · `KEYS` · `URLS` · `PRICES_PER_1K` · `MODELS`.
So enabling ANY registered engine is pure env; an entry missing its requirement is skipped;
the whole toolset is **absent when the chain is empty** (ADR-024's absent-beats-broken); and
only a genuinely NEW wire shape ever touches code — one ~15-line function + one registry
entry, in one file. **Zero new dependencies**: each engine is one httpx call.

**Eight wires ship** (shapes verified against official docs 2026-08-24; the wire file quotes
its sources). Real SERP engines: **Brave** (the only affordable independent index — own
30B+-page crawl, flat $5/1k, clean ToS) · **Tavily** ($8/1k, agent-native, free dev tier) ·
**Exa** ($7/1k, own neural index; `contents.text` piggybacks snippets on the search call) ·
**Perplexity** (the standalone Search API, flat $5/1k, no token costs) · **Parallel**
(parallel.ai v1 GA — the beta path and `processor` vocabulary migrated away in 2026; `mode:
fast` is the $1/1k tier) · **SearXNG** as the self-host/dev/air-gapped engine (AGPL fine at
arm's length — we speak its HTTP API, ship none of it; `just searxng` stands one up).
**Provider-native engines**: **anthropic** (one `claude-haiku-4-5` turn with the server-side
`web_search_20250305` tool — $10/1k, errored searches unbilled) and **openai** (Responses API
`web_search`, `tool_choice: required`, default `gpt-4o-mini` — the verified-supported cheap
model). §9 declined provider-native search as *the agent's hands*; behind our own seam it is
a legitimate *engine* — the agent still holds one uniform `web_search` on every model, and
**both ride the worker's existing provider keys when no search key is set: real web search
with zero new accounts.** Both are LLM-mediated (snippets come from citations), and their
small token sliver rides the provider invoice unmetered — the `PRICES_PER_1K` override is the
recorded dial to fold it in. **The request shape follows the configured MODEL** (fixed
2026-08-24, review): `MODELS` is config, so pinning `web_search_20250305` + `max_tokens: 1024`
in code was right for the shipped haiku default and wrong for anything an operator might name —
`web_search_20260209` is the current variant on Opus 4.6+/5, Sonnet 4.6+/5 and Fable/Mythos,
which also run adaptive thinking, where a 1024-token ceiling truncates the turn. One table
(`_anthropic_search_shape`) keeps the pair together. The `$25/1k` openai default is the
`gpt-4o-mini` rate this wire ships with; the `$10/1k` in the Context paragraph above is the
newer-model tier — two true numbers, and the ledger carries the one the default actually spends. Declined, in the research doc: Serper/SerpAPI (unsanctioned
Google scraping — the Bing lesson priced in), Jina (token-denominated billing muddies
per-query attribution), Kagi (mid-priced, no SLA story), Google PSE (closed), Firecrawl
`/search` (an undisclosed upstream inside a dependency). Engine URLs are **operator
infrastructure** — like `s3_endpoint` they may be private or localhost; the egress guard
(ADR-046) governs model-authored URLs, and an engine endpoint is not one. And for anything
not registered: a self-hosted SearXNG already aggregates 200+ upstream engines behind the
searxng wire — the universal adapter escape hatch.

**Every query is a ledger row.** `Unit.SEARCH` joins the native units; `meter.flat_emitter`
(beside `units_emitter`, same module — the ledger's shape stays owned by one composer file)
writes a `UsageRecord` per successful query: provider = the engine, model = `web-search`,
family = **`"web"`** (family is data — search spend appears as its own line in every by-family
chart, the admin Spend screen included), `cost_usd` from the conf per-1k price at time of
spend, `price_version=0` as the recorded marker for conf-priced-not-PriceBook. The winner
bills; a failed engine never does (the media lane's law). *Amended 2026-11-11:* "failed" means
the provider refused or never got the request (4xx/5xx, connect errors). A query it RAN bills
even when we lost the answer - a 2xx with no results, or a read timeout after the request was
sent (most provider-native searches on one live task timed out at 15 s, each run and invoiced
upstream, none on our ledger). "Sent" is made true by the clock, not tracked: the connection
(DNS included) gets half the query's allowance and gives up as a free connect timeout, so a cut
at the full allowance - the engine's timeout, or the run's wall less its margin - bills. A cancel
from outside (the run itself stopping) bills nothing. Provider-native wires also get a 60 s
read-timeout floor (`WireSpec.min_timeout_s`): their search is a model turn.

**Failure doctrine, verbatim from the media lane:** a search failure costs the search, never
the step. Engines are tried in configured order; each failed attempt is journalled `ok=false`
with the engine's name; an exhausted chain returns an *instructive string* — proceed from the
URLs and material in hand — never an exception, never a `ModelRetry`.

**The content law, applied to queries:** the query is content. It never reaches a journal
summary or a live label (Rule 9; the `generate_image` prompt's line) — nor a `denied_reason`,
which the first cut missed (fixed 2026-08-24, review): `raise_for_status()` builds its message
from the request URL, and a GET engine puts the query there, so every 429 wrote it to the record.
Falling down the chain is the DESIGN, which made this the common path rather than a rare one.
Wires now raise `{engine} answered {status}` and the warden scrubs URLs out of every reason. The journal carries the
engine name; the API key rides a header and nothing else. Result titles and snippets are
somebody else's words — control-stripped, snippet-capped, fenced as *data, not instructions*.

**Search finds; fetch reads.** A result's URL appears in the tool result, which puts it in the
run's context — exactly what ADR-046's context anchor requires, so `web_fetch` can read any
listed result with no new machinery. The composition was designed, not discovered: snippets
are deliberately capped at 400 chars so the model learns to fetch the page rather than cite
the teaser. Granted beside `web.fetch` to the researcher and writer, and to bot authors via
`BOT_TOOLS`.

## Consequences

Zero migrations (family is a string column; units are JSONB), zero new packages, zero new
event kinds. Ops enablement is two env vars — dev/self-host can run entirely on a local
SearXNG (`COMPUTER_SEARCH_ENGINES=searxng` + `COMPUTER_SEARCH_URLS=searxng=…`); production
adds `brave` (and/or `tavily`) via `COMPUTER_SEARCH_KEYS=brave=…` when the account exists.
There is no `COMPUTER_SEARCH_SEARXNG_URL` — the four flat grammars are the whole surface,
and an engine whose requirement is unmet is skipped, which with a one-engine chain means
the tool silently does not exist. Absent-beats-broken only helps an operator who can tell
the two apart, so the name has to be right everywhere it is written (corrected 2026-08-24). Until an operator sets
`COMPUTER_SEARCH_ENGINES`, nothing changes anywhere — the tool does not exist.

**Amended 2026-11-11: the planner hears the engine, and a dial yields to it.** With an engine
configured, the planner's brief still said "no step can read the internet" (its sources section knew
only the device's browser), and the researcher's roster entry reads "any URL the task names". Both
undersold the lane: a sourced spreadsheet was planned as one modeler step with search live. Now the
brief reads `planning_brief.web_reach`: with an engine, the sources section stops saying no step can
reach the web, and the entry of every agent granted `web.search` (not the computer, whose entry
says how it uses the web) carries one more line, "Searches the web here", derived from the live
chain and the grant, never hardcoded, so it is absent whenever the tool is. And a user's deliverable
dial, which is otherwise the whole plan, yields to the planner when the ask wants what the web says
now (`ai/policy/web_facts.web_facts_cue`: currency words beside a fact noun, a request to cite, a
request to search) and the dialed agent cannot search for it here; the planner then seats a step that
reads the web before the dialed kind's agent (a researcher with an engine, the computer without one,
ADR-078 decision 20 as amended). The planner book (`evals/planner_book.py`) holds both shapes.

**Amended 2026-11-14: one call runs a batch.** `web_search(queries, n)` takes one to five queries;
they run together, each down the chain on its own client, and still bill one `SEARCH` unit and write
one journal row per engine asked, the engine's name its summary and the query in neither. The results
merge by rank, a page several queries found is listed once with the query numbers that found it, and
a row carries the engine's publish date (YYYY-MM-DD) when the wire returns one (brave, tavily, exa,
perplexity, parallel, searxng). A repeated query in the batch runs once; queries past the five or past
the step's budget are not run, and the result says how many.

**Amended 2026-11-14: the makers read the web, so the dial keeps its step.** Who holds `web.fetch` and
`web.search` is one table, `ai/agents/grants.py`, and the mounts read it. The file makers (coder,
modeler, presenter, typesetter, organizer) and the operator now hold both, and the judge holds
`web.fetch` (the context anchor admits only URLs the work names). Every agent a dial seats can reach
the web itself, so the dial's yield and its cue (`web_facts_cue`) are deleted: a dialed ask for current
facts stays the dial's one step. With no engine configured, that step reads only the pages the ask
names; the planner brief's reach paragraphs list the agents that hold `web.fetch` from the table.

Named and still deferred: the browse lane (playwright + crawl4ai) for JS-rendered pages;
Exa as a possible *additional* semantic engine behind the same seam; the `web-research`
skill (the skills loop, taught once both web tools are live in production config).

**Amended 2026-11-15 (ADR-092 §8, the v1 doors deleted):** The answerer is deleted: the Lead holds `web_search`, and a Bot's conversation reply holds it through the `grants.TALK` row.
