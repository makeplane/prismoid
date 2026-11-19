# ADR-015 — Artifact kinds: one registry on the wire, views as lenses, edits as revisions, skills as the authoring surface

*Status: accepted · July 2026 · **M1–M5 landed** (five commits, one per milestone); M6 lanes recorded below*

## Context

Artifacts were markdown-only at the presentation layer while competitors ship native docs/Excel/
slides as deliverables. The product wants (1) **structured view artifacts** — board / table /
gantt / calendar / gallery — rendered natively and **editable** (drag included); (2) office-format
fidelity; (3) a teammate able to add a new artifact kind in ~a day; (4) skills as the authoring
surface for kinds.

What exploration found: the registry already existed (`schemas/outputs.py` — `ArtifactType` +
`ArtifactTypeSpec` + `accepts()` at the publish door), and native xlsx/pptx production **and
verification** already worked. The rot was drift and dishonesty around the registry, not a missing
one. **Five independent format lists** had grown (the registry; `frame_tools.TEXT_SUFFIXES`; the
publish tool's hand-typed docstring, which advertised a dataset without `.jsonl`; the web's
`PREVIEWABLE` set, which listed types that do not exist and piped `.docx` zip bytes through the
markdown renderer; the web's icon map). Plus a defect inventory: publishing a `.csv` crashed the
step (`SuiteNotBuilt` uncaught — reproduced live before the fix); the planner typed **every**
publishing contract `document` so modeler/presenter/coder contracts lied to the verify judge;
`ManifestEntry.skill_pins` was never stamped; the judge was shown raw zip/tar bytes for binary
"text-type" artifacts.

External patterns adopted: views are **lenses over one dataset** (Notion/Airtable; Plane's five
layouts); office formats are a **skills + sandbox + validator** problem (Anthropic's document
Agent Skills); the Agent Skills format is an open standard our SKILL.md already anticipates
(`references/` appears in our own error text); MCP-Apps-style interactive iframes are a future
lane, recorded here, not built.

## Decision — the doctrine

Mirrors `capability.py`'s class-vs-family asymmetry:

- **A kind is code**: an `ArtifactType` enum member + `ARTIFACT_TYPES` row + a verifier suite +
  a web renderer — a small honest deploy, done by recipe (below). `Artifact.type` is
  `String(24)`, so a new kind needs **no migration** (ADR-010's precedent).
- **How a kind is authored well is data**: a skill (schema reference, gotchas, an in-frame
  validate script) — draft → test → publish behind the 409 evals gate, pinned into plans,
  stamped into provenance.
- **One source of truth, exported.** The registry answers presentation questions **server-side**:
  `ArtifactTypeSpec.text_extensions` (a type can be text; only a *file* is text) and
  `render_for(type, filename) → RenderHint` (`markdown | text | none`, later `collection`).
  `ArtifactOut` carries `filename` + `render`; web and CLI dispatch on the wire and keep **no
  client-side format lists**. The streaming gate and the publish docstring are derived from the
  registry and drift-tested.
- **Honesty over green.** A missing suite is caught (`suite:built` SKIP) and ships the artifact
  `unverified`, loudly — never a crash, never a vacuous pass. `format:*` joins the structural
  check prefixes: well-formed is not verified. Binary documents skip the prose checks with
  `format:extraction` SKIP instead of "verifying" mojibake; `_deliverable_view` shows the judge
  bytes only when the actual file is text.
- **Contracts are typed by the agent.** `AgentDef.produces: ArtifactType`; the planner receives
  the roster as `Mapping[name, ArtifactType]` (policy stays below the registry) and stamps
  `OutputRef.type` + the `publishes_clause(type)` acceptance line from it. The proposal cannot
  choose a type; the planner brief names what each agent publishes.
- **Provenance completes at harvest.** `skill_pins` are stamped beside `agent`/`model` from the
  plan step — the frame honestly writes `[]` because the frame cannot know.

**Amended 2026-11-15 - how a skill reaches a run.** A plan pins every skill to a version when it is
written, a format maker's format skill included (`operations/planning_steps.write_steps`), so every
attempt runs the same one. A pin is honoured only where the step's agent holds every tool the skill
declares in its `allowed-tools` (`grants.skill_fits`, applied by `operations/skills.honoured` before
anything mounts); a declaration grants nothing, and a dropped pin is journaled with the tools it
lacks. A frameless agent (the answerer, the Lead) sees the skills `grants.SKILLS` gives it as a menu
frozen for the run (each name, version and description, sorted by name, each body by its content
hash) in the `skill_read` tool's description, so the menu sits in the cached prefix; it reads a
body only when one fits, and a name off the menu is refused as a result.

## The milestones (designed here, all landed 2026-07-31 — details in STATE.md's entries)

- **M2 `collection`** — ONE new kind (`.collection.json`; the compound suffix works through
  `accepts()` today and streams as text): `collection_version`-gated pydantic model in
  `schemas/collection.py` = fields + rows + named views (`table board list gallery gantt
  calendar`; `group_by`/`sub_group_by`/`sort`/`filter`/date fields/`cover_field`/`columns`).
  Layouts are lenses; a new layout is a renderer + schema rev, never a new kind. A new
  `organizer` agent produces it; a stdlib in-frame suite mirrors the pydantic model, pinned by a
  **lockstep mutation test** (one definition; the mirror cannot drift). Web renders it with
  zero new deps on the kit; the JSON draft streams as a quiet "building — N rows" card, never
  JSON soup.
- **M3 editing & revisions** — artifacts stay append-only: an edit mints an immutable new
  version. `artifacts` gains `lineage_id` / `revision` / `supersedes_id` (the one collection
  migration); `POST /v1/artifacts/{id}/revise` validates content against the same pydantic
  model, requires the base sha (409 + latest on conflict), stamps `agent="user"` provenance,
  emits durable `artifact.revised`. Display paths show lineage-latest; GC walks every revision;
  each version's verification is its own (a human revision renders "edited · unverified").
  Agent revisions ride `follow_up` onto the same lineage. Drag-and-drop (board/calendar/gantt/
  reorder) is a **cell edit in disguise**, built on `@dnd-kit` (ADR-016) inside a lazy editor
  chunk; every drag has a non-drag equivalent. The task board's no-drag stance is untouched —
  that is about task *status* belonging to the machine; collection cells are user data.
- **M4 docs/sheets fidelity** — `.docx` verification via python-docx text extraction (frame-pinned
  already) feeding the same prose pipeline; xlsx in-app preview as a **sidecar**: the suite writes
  `/verify/preview.json` (64 KB cap), operations stores it as a CAS blob on
  `artifacts.preview_sha256` (nullable column; second migration), served by
  `GET /v1/artifacts/{id}/preview` — never inside the signed report.
- **M5 skills carry assets** — `scripts/ references/ assets/` (open Agent Skills alignment),
  per-file CAS blobs listed in the manifest JSONB, mounted read-only under `/skills/<name>/`;
  the `organize` skill becomes the kind-authoring template (schema reference equality-pinned to
  `model_json_schema()`, validate script lockstep-tested). **The 409 publish gate stays
  routing-only** — golden runs are nightly, advisory; gating publish on multi-minute real-money
  runs would kill the sub-minute edit loop. Do not "fix" this.

## The recipe (add a kind in a day)

1. `schemas/<kind>.py` — pydantic content model (skip if binary).
2. `schemas/outputs.py` — enum member + `ARTIFACT_TYPES` row (+ `RenderHint` if new).
3. `ai/verification/suites/<kind>.py` + `pipeline.SCRIPTS` row (+ lockstep test if structured).
4. Producer: `produces=<KIND>` on an existing agent, or one new agent file + one import.
5. `web/app/components/artifact/renderers/<kind>-view.tsx` + one `RENDERERS` line
   (+ optional `editor` thunk). The shell owns fetch/boundary/fallback.
6. `backend/skills/<skill>/` (SKILL.md + cases.yaml + references + validate script) → seed →
   test → publish.
7. `just export-contract && just web-gen`; a golden case; one real proof run.

## Dependencies

**Zero added** to the backend venv, the web bundle, and the frame image through M1/M2/M4.
Avoided, with the mechanism that avoids them: pandoc (python-docx suffices for extraction),
pyarrow (parquet magic-bytes only, said in the report), openpyxl-in-worker (preview generated in
the verify frame), pypdf (deferred until PDF extraction is worth an image rebuild), TanStack
Table (only at >10k rows or grid editing), gantt/calendar widget libs (never — they own their
pixels and fork the design system). The single planned exception: `@dnd-kit` for M3 editing
(ADR-016 + HANDOFF §3 row when it lands).

## Consequences

- `ArtifactOut` gained `filename` + `render`; the events enum widens when `collection` lands —
  both ride the `just export-contract` + `just web-gen` ritual, web façade updated in the same
  commit.
- The web's `PREVIEWABLE` and the mojibake path are gone; unknown/binary kinds land on the
  `DownloadCard` floor (`components/artifact/renderers/` — layer row added to `web/DESIGN.md`).
- Known unused capacity recorded, not deleted (repo rule: nothing thrown away without a
  reason): `OutputRef.unverified_ok`, `SmokePolicy` (declared, never read — webapp browser smoke
  does not exist), `StepKind.ARTIFACT` (never constructed), `register_output()` (the natural
  seam if kinds ever become rows). Sandbar/FC rootfs still lacks python3 for publish/verify —
  pre-existing, tracked by ADR-013/C2, unchanged here.
- Two migrations total across the staged work (M3 lineage, M4 preview) — none in M1/M2/M5.

**Amended 2026-11-15 (ADR-092 §8, the v1 doors deleted):** A kind is never read from the request's words any more (`ai/policy/kinds` is deleted): it is the person's dial, or the plan's own stamp after commit.
