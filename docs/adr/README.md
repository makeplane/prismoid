# Architecture decision records

Each file here records one design decision: the context, what was decided, and why. They are
numbered in the order they were written and are not rewritten afterwards; a later record amends or
supersedes an earlier one and says so. Code comments and the folder `CLAUDE.md` files cite them as
"ADR-n".

Numbers 002-005, 068 and 085 do not exist in this repository, and 035-044 are reserved and unused. ADR-099 rewrote the product's name in every record in place; records written before ADR-067 use the other retired names: Computer (now Prismoid),
Organization (now Workspace) and Space (now Project).

| ADR | Title | Status |
|---|---|---|
| [001](001-architecture.md) | Layer-first architecture, and the two seams it forces | Accepted |
| [006](006-api-drift.md) | Library-vs-sketch API drift | Living document: where the code follows a library instead of the original sketch |
| [007](007-service-split.md) | Service split: the backend becomes `backend/`, the web app stays `web/` | Accepted |
| [008](008-continuous-operation.md) | Retire the Phase-1 build protocol; operate continuously | Accepted |
| [009](009-scale-posture.md) | Scale posture: what scales today, what waits for a trigger | Accepted |
| [010](010-fleet-mode.md) | Fleet mode: every model answers; nothing is forced to agree | Accepted |
| [011](011-pgvector-retrieval.md) | Memory retrieval: full-text search now, the pgvector hybrid pre-planned | Deferred until its triggers are met |
| [012](012-cloud-context-and-storage.md) | Deployment posture: context, storage, and frames, sovereignty-first | Proposed (provider-neutral revision) |
| [013](013-sandbar-microvm-frames.md) | sandbar: microVM frames as an independent subproject | Accepted (the Firecracker backend remains experimental) |
| [014](014-workspace-tenancy.md) | Workspace tenancy: deferred, with the retrofit path recorded | Superseded by ADR-061 |
| [015](015-artifact-kinds.md) | Artifact kinds: one registry on the wire, views as lenses, edits as revisions | Accepted |
| [016](016-dnd-kit.md) | @dnd-kit for collection editing gestures | Accepted |
| [017](017-serif-heading-font.md) | The serif heading trial, reverted; Inter rendered right | Reverted |
| [018](018-follow-up-continuation.md) | A follow-up continues the task; fork is the only branch | Accepted |
| [019](019-answer-first-verification.md) | The answer is primary; verification is its receipt | Accepted |
| [020](020-connectors.md) | Connectors: external MCP servers as user-attached tool surfaces | Accepted, amended by ADR-054 |
| [021](021-document-workshop.md) | The document workshop: format skills, the typesetter, and a real PDF lane | Accepted |
| [022](022-the-reader-renders.md) | The reader renders every kind: in-frame preview sidecars, native PDF, the sandboxed page | Accepted |
| [023](023-full-fidelity-renditions.md) | Full-fidelity renditions: LibreOffice in the frame, the document as the view | Accepted |
| [024](024-model-plane.md) | The gateway grows a model plane: media lane, provider discipline, quality routing | Accepted, amended by ADR-072 |
| [025](025-longform-lane.md) | The long-form production lane: outline, parallel sections, assembly | Accepted |
| [026](026-qa-lane.md) | The QA lane: a pre-delivery editorial gate | Accepted |
| [027](027-workflow-plane-robustness.md) | Workflow-plane robustness: ghosts, recovery, waves | Accepted |
| [028](028-consensus-plane.md) | The consensus plane: typed joins, the harmonize wave, the master consensus, the panel | Accepted |
| [029](029-universal-size-buckets.md) | Universal size buckets: five sizes, every artifact kind | Accepted |
| [030](030-figure-pregeneration.md) | Figure pre-generation: the outline is the index; the images fly in parallel | Accepted |
| [031](031-password-reset.md) | Forgotten passwords: an emailed single-use code, and the product's first outbound mail | Accepted |
| [032](032-deliverable-path-law.md) | The deliverable path law: dial over language over planner, stamped and shown | Accepted |
| [033](033-revision-control-everywhere.md) | Revision control everywhere: restore, text editing, named versions | Accepted |
| [034](034-the-formatting-bar.md) | The formatting bar: one house look, and a gate that looks at the page | Accepted |
| [045](045-bots-and-one.md) | Bots: user-authored agents as data, and One, the front door that routes to them | Accepted |
| [046](046-the-web-door.md) | The web door: `web_fetch`, the egress guard, and the document sidecar | Accepted |
| [047](047-the-search-lane.md) | The search lane: `web_search`, engines as config, queries on the ledger | Accepted |
| [048](048-routines.md) | Routines: standing work, the third axis | Accepted |
| [049](049-triage-and-the-express-lane.md) | Triage at the door: the express lane, per-turn lanes, and the answer budgets | Proposed; built and in use |
| [050](050-orchestration-data-plane.md) | The orchestration data plane: the plan is the protocol | Accepted |
| [051](051-the-lane-recipe.md) | The lane recipe: what a lane is, so the next one is rows, not surgery | Proposed |
| [052](052-the-problem-book.md) | The problem book: graded golden cases and the eval tiers | Proposed; built and in use |
| [053](053-answer-rendering.md) | The answer render: rich blocks, code cards, and the em-dash rule | Accepted |
| [054](054-connector-policy-and-catalog.md) | Connectors v2: the catalog, one-click OAuth, per-tool policy, and the approval gate | Accepted (amends ADR-020) |
| [055](055-the-charter.md) | The charter: one identity, every seat | Accepted |
| [056](056-bots-v2-the-teammate.md) | Bots v2, the teammate | Accepted, amended by ADR-057 |
| [057](057-conversations-talk-is-not-work.md) | Conversations: talk is not work | Accepted, amended by ADR-059 |
| [058](058-talk-until-it-is-work.md) | Talk until it is work | Accepted |
| [059](059-one-talk-engine.md) | One talk engine: a bot talks in one place, and the answer streams | Accepted |
| [060](060-one-thread-the-card-and-the-guardrails.md) | One thread, the card, and the guardrails | Accepted |
| [061](061-organizations.md) | Organizations (now Workspaces): the tenant | Accepted (supersedes ADR-014), amended by ADR-062 |
| [062](062-the-space-is-where-people-work-together.md) | The Space (now Project) is where people work together | Accepted |
| [063](063-the-machine.md) | The Machine, a task's remembered computer | Accepted, amended by ADR-076 and ADR-078 |
| [064](064-safe-point-steering.md) | Safe-point steering with plan epochs | Accepted |
| [065](065-the-screen-door.md) | The screen door | Accepted |
| [066](066-chromium-in-the-browser-frame.md) | Chromium in the browser frame: one pinned build, and the container as its sandbox | Accepted |
| [067](067-the-prismoid-rename.md) | The Prismoid rename: Computer, Organization and Space become Prismoid, Workspace and Project | Accepted |
| [069](069-the-durable-seam.md) | The durable seam: DBOS only through `jobs/durable.py` | Accepted |
| [070](070-the-machine-starts-when-the-task-is-accepted.md) | The Machine starts when the task is accepted | Accepted, amended by ADR-103 |
| [071](071-the-turborepo-monorepo.md) | The Turborepo monorepo | Accepted |
| [072](072-httpx2-for-the-provider-wire.md) | httpx2 for the provider wire | Accepted (amends ADR-024) |
| [073](073-oxlint-and-oxfmt.md) | oxlint and oxfmt replace ESLint and Prettier | Accepted |
| [074](074-propel-design-system.md) | The Propel design system, `@prismoid/blocks`, a black accent | Accepted |
| [075](075-automated-review.md) | Automated review: the ratchet, the rules files, the reviewer | Accepted |
| [076](076-a-sleeping-computer-holds-no-room.md) | A sleeping computer holds no room | Accepted |
| [077](077-prismoid-fully-local.md) | Prismoid fully local (`prismoid up`) | Accepted |
| [078](078-the-device-machine.md) | The device Machine: the user's own Mac | Accepted |
| [081](081-playwright-smoke-flows.md) | Playwright smoke flows against a real stack, and a web coverage floor | Accepted (amends ADR-079) |
| [082](082-stream-tickets-and-release-automation.md) | Stream tickets, and release PRs with release-please | Accepted (amends ADR-031) |
| [083](083-silo-object-store.md) | Silo as the self-hosted object store | Accepted (amends ADR-080) |
| [086](086-open-signup-guards.md) | Open signup guards | Accepted |
| [087](087-a-default-task-cap-and-a-gone-creator.md) | A default task cap, and no work for a creator who is gone | Accepted |
| [089](089-durable-agent-run.md) | The durable agent run: a step attempt on its call log | Accepted (amends ADR-064, ADR-069) |
| [090](090-capabilities-are-the-extension-unit.md) | Capabilities are the extension unit: an agent is data, prompts are named blocks, the request is a reviewed snapshot | Accepted |
| [091](091-four-step-agents.md) | Four step agents and the judge: retired names resolve in one hop | Accepted |
| [092](092-the-lead.md) | The Lead: one model run decides a turn, and the record says how it ended | Accepted (amends ADR-049, ADR-052, ADR-058, ADR-059, ADR-064) |
| [097](097-the-code-frame-profile.md) | The code frame profile: git, ripgrep and node for a code step, layered between base and browser |
| [099](099-prismoid-the-open-source-name.md) | Prismoid, the open-source name: one name in code, env, data and docs | Accepted |
| [100](100-push-notifications-for-the-inbox.md) | Push notifications for the inbox | Accepted (amends the HANDOFF §1 Inbox row) |
| [101](101-json-logs-on-stdout.md) | Logs on stdout are JSON records; spans leave only by OTLP | Accepted |
| [102](102-copyright-headers.md) | Copyright headers on every source file: one notice, one tool, a law and a pull-request check | Accepted |
| [103](103-the-edition-seams.md) | The edition seams: where a distribution adds its own features | Accepted |

## Writing a new record

Take the next free number, name the file `NNN-short-title.md`, and start with a title line and a
status line (`proposed`, `accepted`, `superseded by ADR-n`). Cover the context, the decision, the
alternatives you rejected and the consequences. A new dependency always needs a record, plus a row
in the dependency table in `HANDOFF.md` §3.
