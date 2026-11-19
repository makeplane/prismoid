# ADR-051 — The lane recipe: what a lane is, so the next one is rows, not surgery

**Status:** proposed · 2026-08-27 · written on the owner's direction, same session as
ADR-049/050: *"a bigger Perplexity-Computer… more lanes like Code, Design, and heavy
lifting like Bioscience… EXTREMELY extensible for me to create different kinds of lane"* —
under the range invariant: **a new heavy lane may never cost the simple question its
speed.**
**Grounding:** the Computer X extension ladder (2026-08-22: L0 prompt → L1 pack → L2 kind
→ L3 lane → L4 surface; *"a domain climbs only as far as its ground truth demands"*), the
software-factory blueprint (ADR-035–044 reserved — the code lane this ADR gives a socket
to), ADR-049 (the door that composes lanes), ADR-050 (the data plane inside them), and the
seam map in the unpublished research note behind ADR-049, §1.

## Context

Plane runs five lanes today and every one is a bespoke branch: the DM lane is an `if` in
`draft_plan` (`operations/planning.py:256`), fleet is the next `elif` (`:290`), sectioned
the next (`:323`), the dialed path the next (`:368`), and express (ADR-049) is about to
become the fifth. Each grew its own door, its own plan shape, its own verify posture — and
that organic growth was correct: three lanes was too early for an abstraction. Eight is
not. The blueprint alone brings code (ADR-035–044); the owner names Design and Bioscience
behind it. Without a recipe, every new lane is spine surgery performed by whoever builds
it; with one, a lane is a registry entry and the spine is never opened.

The recipe also carries the constitutional load: **the range is the product.** The reason
"what's the time?" must survive the arrival of a Bioscience lane is structural only if
lane entry is centrally ordered (triage's precedence, ADR-049 §2) and every lane ships its
budgets into one gate (`just range`). A lane that could claim the trivial end, or regress
another lane's budgets unnoticed, is exactly the haywire the owner named.

## 1 · A lane is one registry entry with seven fields

`ai/policy/lanes.py` — the `agents/all.py` pattern: one file, one screen, the import IS
the registration. `LaneDef`, frozen:

1. **The door** — detector predicates (pure functions over turn text + dials + thread
   state) and the dial claims that force the lane (`mode=fleet`, `direct_bot`,
   `scale ≥ extended`…). Threshold *values* live in conf, never in the predicate — the
   Bedrock dial rule (ADR-049 §1.3).
2. **The plan shape** — one pure policy function `(goal, inputs, catalog-facts) → steps +
   edges + criteria`, the `fleet_plan` / `longform_plan` / DM one-step shape. Deterministic
   lanes own their whole shape; the standard lane's shape is "run One" — the planner is a
   lane's shape like any other.
3. **The roster slice** — which agents (and whether bots/connectors) the lane may seat;
   `plannable` and the deps-population rules already enforce the edges by construction.
4. **The priming pack** — what mounts on entry, lazily, with receipts (ADR-049 §8):
   transcript-only (express) · context pack + skill pick (standard) · the bible
   (sectioned) · fenced briefs (scatter) · rules files + repo map + warm workspace rows
   (code). Lane state lives on rows, so re-entry is warm; honing is state + receipts,
   never process memory.
5. **The budgets** — the lane's §15 rows (TTA p50/p95 + its stage budgets) and its
   `just range` prompts. **A lane without budget rows does not register** — the gap
   ADR-049's audit found (temporal budgets asserted nowhere) is closed at the recipe
   level: you cannot add a lane without adding its gauge.
6. **The verify posture** — `receipt` (ADR-019, standard) · `inline` (ADR-010 fleet;
   ADR-036 code) · `qa-gate` (ADR-025/026 long-form) · `none` (express — nothing
   published). The field that has already legitimately varied four times is the proof the
   recipe seam is real.
7. **The surfaces** — the lane chip + progress copy rows (the web's `progress-copy.ts`
   grammar), and the CLI's `--json` fields. A lane is visible or it does not exist.

**What is deliberately NOT in the recipe: the spine.** The run_task loop, the queues, the
event lanes, the ledger, the Bursar, the warden, gates/steering/turns — a lane composes
them and may never edit them. A lane that needs spine surgery is not a lane; it is an ADR.

## 2 · Triage composes lanes; precedence is central

The registry feeds ADR-049's door: **dial > deterministic > classifier > planner-anchor**,
evaluated over registered lanes in a **centrally-owned precedence order** — a lane
registers detectors but never its own rank. Two consequences, both constitutional:

- **The express floor**: no lane may claim the trivial end. Express's detectors run first
  because the center says so, which is how a Bioscience lane is structurally unable to
  swallow "what's the time?" — the answer to the owner's invariant is an ordering, not a
  hope.
- **The anchor**: abstention resolves to standard (the planner's judgment), never to any
  lane's door — a new lane misclassifying costs politeness, never a wrong commitment.

## 3 · The range invariant, as a gate

`just range` (ADR-049 W1) holds one canonical prompt battery spanning the whole range —
trivial · lookup · brief deliverable · standard · sectioned · (scatter · code · … as they
land) — each row asserting its lane's budgets from the record's own timestamps (the
`*.dequeued` events, W0). **Every new lane ships its rows in the same change that
registers it, and a lane that regresses ANY other lane's rows is red.** The drill rides
the nightly perf cadence beside the >20%-regression gate. This is the difference between
"we care about the range" and the range being load-bearing.

## 4 · The instances, mapped

| Lane | Door | Shape | Verify | Status |
|---|---|---|---|---|
| express | detectors + intent phrases (never a dial) | code-made frameless one-step (ADR-049 §3, three tiers) | none / receipt on the publish tier | ADR-049 W1 |
| standard | the anchor | One proposes, code disposes | receipt (ADR-019) | live |
| direct | `direct_bot` dial | code-made one-step, planner unreachable | by kind | live |
| fleet | `mode=fleet` dial | `fleet_plan`, pinned seats + chair | inline (ADR-010) | live |
| sectioned | scale dial / deterministic size | architect → expand → sections → harmonize → assembly | qa-gate (ADR-025/026/028) | live |
| scatter | triage `retrieval_need=research` / dial | map → `swarm.expand` → fenced seats → synthesis | receipt on the synthesis | ADR-050 W2 |
| code | repo dials (`repo_id`, `base_ref`…) | scout → replan → implement → integrate | inline (ADR-036) | blueprint 035–044 — this ADR is its socket |
| design | named future | (its own ADR when its ground truth demands a lane — it may prove to be a kind + pack, L1/L2, first) | — | future |
| bioscience | named future | per the beast ruling: a thin, versioned, disposable `HarnessSpec` behind one seam — never selling the loop, never outsourcing governance | — | future |

The ladder rule stands guard over the last two: a domain climbs to L3 only when it has
state that IS the work (code's workspace; bio's instruments). Design may well live as an
L2 kind + L1 pack for a long time — the recipe makes that cheap to find out.

## 5 · Rejected, each with its reason

- **Lanes as database rows** (the models/skills/bots pattern): a lane carries *policy
  functions* — plan shapes and detectors are code that must be reviewed, tested, and
  versioned with the engine. The data half (thresholds, budgets, prompts of the range
  battery) IS data and lives in conf/fixtures. Rows for behavior was ADR-045's line too:
  everything a user composes becomes data; everything that must be safe stays code.
- **Per-lane execution engines**: the constitution, twice over (ADR-048: "no second
  execution path"; Zapier/Make spent years rebuilding budgets/history/permissions their
  chains forked away from). A lane is a plan shape over the one spine.
- **Plugin-shipped lanes**: bots and packs (L1) are the extension surface for users; lanes
  are operator/engineering surface. Revisit only after the recipe has served the in-house
  lanes — the marketplace question is ADR-014 tenancy's, not this ADR's.
- **A generic "lane framework" with hooks everywhere**: the admin console already refused
  the Django-generic trap once (2026-08-23: "every such framework dies by escape hatch").
  Seven named fields, one registry, the spine closed — that is the whole framework.

## Consequences

- **Zero new dependencies, zero migrations.** The registry is one policy file; the first
  build (ADR-049 W1) adds express as a branch beside the existing four and the
  **extraction of the branch chain into the registry is this ADR's own W1** — mechanical,
  behavior-preserving, proven by the suites staying green.
- **CLAUDE.md gains** (on acceptance): *"Lanes (ADR-051): a lane is one `ai/policy/lanes.py`
  registry entry — door · shape · roster · pack · budgets (+ `just range` rows, mandatory)
  · verify posture · surfaces; precedence is central (the express floor is
  constitutional); the spine is never edited by a lane."*
- The doctrine sentence: **a new lane is a registry entry with a gauge; the spine does not
  open.**

## Workstreams

- **W1 — the registry extraction**: `LaneDef` + `lanes.py`; fold the `draft_plan` branch
  chain into a registry walk (byte-identical plans; the suites are the proof); ADR-049's
  express registers rather than branches. Exit: `just check`/`just test` green with zero
  plan-shape diffs on the golden tasks.
- **W2 — the range battery formalized**: the canonical prompts move from ADR-049's drill
  script into per-lane fixture rows the registry itself enumerates — registering a lane
  without rows fails a doctrine test. Exit: `just range` derives its battery from the
  registry.
- **W3 — the code lane lands as the first external consumer** (with ADR-035–044): the
  proof that the socket fits is that the blueprint plugs in without a spine edit; any gap
  it finds amends THIS ADR with its reason.

## Open questions — builder's calls, taken 2026-08-27, reversible

1. **Registry now vs after express**: express ships first as the fifth branch (ADR-049 W1,
   smallest honest diff), the extraction follows as ADR-051 W1 — behavior-preserving
   refactors ride best on green suites, not mid-feature.
2. **Design's altitude**: entered in the map as "probably L2/L1 first"; its own ADR
   decides when its ground truth speaks.

**Amended 2026-11-15 (ADR-092 §8, the v1 doors deleted):** The scatter door is deleted (`scatter_ask`); scatter is a Lead recipe step expanded at commit into the map step. Its shape, validator, expansion and tally moved from `ai/policy/swarm` to `operations/swarm`; the synthesis takes over every edge that waited on the step, and a recipe step at the end of a plan delivers through its synthesis.
