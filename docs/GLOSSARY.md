# Glossary

The words Prismoid's code, API and docs use, in plain terms. Where a term has a home in the code, the
path is given (relative to `backend/prismoid/` unless it says otherwise).

Older decision records and comments sometimes use retired names: **Computer** for Prismoid,
**Organization** for Workspace, **Space** for Project, and **Plane** for the system's own voice.
They mean the same things.

## Work

| Term | Meaning |
|---|---|
| **Task** | What a user asked for: a goal, plus its dials (deliverable, format, budget, mode). The unit of work and of billing. `db/models/task.py`, created by `operations/tasks.create_task` |
| **Turn** | One round of a task. A follow-up is the same task's next turn, with the whole thread and the earlier artifacts mounted |
| **Fork** | The only way to branch: a child task that carries the parent's conversation and dials, born held |
| **Talk or work** | The Lead decides each turn by how it ends it: a reply is talk; a plan is work. No code reads the words to decide. `operations/turn/`, `ai/agents/lead.py` |
| **Plan** | The graph of steps a planner agent draws for a work turn. `operations/planning.py` |
| **Step** | One node of the plan: one agent, one goal, its dependencies. Steps whose dependencies are done run in parallel |
| **Frontier** | The set of steps whose dependencies are all done. The task workflow runs one frontier, then asks again |
| **Lane** | A specialised path for a kind of work: the express lane for trivial answers, the long-form lane for long documents, the scatter lane for wide research, the action lane for one service write, the QA lane before delivery |
| **Fleet mode** | Every enabled model answers the same step independently; a chair agent tabulates where they agree and differ |
| **Deliver** | The end of a turn: the answer is written for someone who was not watching, recorded and sent to their inbox |
| **Verification (receipt)** | After delivery, the `verify_artifact` job re-checks each artifact and signs the verdict with Ed25519. It never blocks or fails the answer. `operations/verification.py` |
| **Capture** | A soft intake: a note or link saved for later, not yet a task |
| **Inbox** | The system's messages to a user: delivered tasks, questions waiting, broken connectors; pushed to their phones too (ADR-100) |

## Asking, steering, stopping

| Term | Meaning |
|---|---|
| **Gate** | A durable pause where a task waits for a person at near zero cost. Kinds: `input` (one numbered batch of questions), `approve`, `budget`, `connect` (a service to link), `pause`, `control` (a person at the screen). `schemas/plan.GateKind` |
| **Resolve** | Answering a gate: accept, decline or cancel. Unanswered gates time out to a stated policy |
| **Steer** | A mid-flight instruction ("price it in euros"). Applied at the next safe point; unstarted steps re-plan |
| **Safe point** | Where a steer, pause or cancel can land without corrupting work: before a model request, before a tool call, at the end of a step's run, between frontiers. A tool already running always finishes |
| **Plan epoch** | A counter on the task bumped by every accepted steer and turn. A steer sent against an older epoch is refused (`409 stale_steer`) so teammates do not overwrite each other |
| **Budget** | A task's spending cap. The engine stops before an overrun and opens a `budget` gate |

## Engine

| Term | Meaning |
|---|---|
| **Job** | A durable background workflow, registered in `jobs/all.py`: `run_task`, `run_step`, `verify_artifact`, sweeps and others |
| **Effect** | One journaled unit inside a job (`@durable.effect`). After a crash, finished effects are not re-run. All DBOS use goes through `jobs/durable.py` |
| **DBOS** | The library that makes jobs durable on Postgres (queues, journals, recovery). No separate queue server |
| **Operation** | A function in `operations/` that owns a database transaction and composes rows, agents and jobs. Endpoints and jobs call operations |
| **Agent** | A role (planner, writer, maker, judge, ...) defined as an `AgentDef` in `ai/agents/`, with a prompt and a minimal tool grant. Never a business rule |
| **Model class** | What a step needs from a model, without naming one: `fast-executor`, `frontier-reasoner`, `long-context`, `cheap-classifier`, `code`, `vision`, `judge`, `image-gen`, `computer-use`. `schemas/model_class.py` |
| **Model catalog** | The table of models, their classes, protocols and prices. Adding a model is a row, not a deploy (`prismoid models add`) |
| **Routing chain** | The ordered models that serve a model class; a model that cannot serve falls through to the next |
| **Gateway** | The model plane in `ai/gateway/`: builds a model from a catalog row, throttles per provider, meters every call |
| **UsageEvent / ledger** | Every provider call, search query and Machine-second is recorded in native units with its US dollar cost. The ledger is append-only and attributes spend at the time it happened |
| **Allowance** | A workspace's spend allowance (`operations/allowance.py`, ADR-103): the seam where a distribution caps what a workspace may spend. None in this edition: every door opens |
| **Tool** | A function an agent may call (write a file, run a command, fetch a URL, search). Mounted only when granted; every call passes the `tool_record` and approvals capabilities |
| **Tool record** | `ai/capabilities/tool_record.py`: journals every tool call once as `tool.called` (argument keys, never values) |
| **Skill** | Packaged guidance an agent loads for a kind of work (`backend/skills/<name>/SKILL.md`). Draft, test, publish; publishing needs green evals |
| **Evals / problem book** | Scored cases run against real models (`just evals`). Problem books are collections of real misroutes that must stay fixed |

## Machines

| Term | Meaning |
|---|---|
| **Machine** | A task's remembered computer: one per task, created when the first step needs one, shared by later steps and turns, put to sleep when idle. `db/models/machine.py`, `operations/machines/` |
| **Frame** | The sandbox a step's agent actually works in, as the engine sees it (`ai/frames/`). A Machine wraps a frame; verification and QA use throwaway frames |
| **Driver** | Which technology provides frames, set by `PRISMOID_WORKER_FRAME_DRIVER`: `docker` (local containers), `sandbar` (the sandbox daemon), `device` (the user's Mac). |
| **Profile** | What a frame carries: `base` (documents, spreadsheets, code) or `browser` (adds Chromium) |
| **sandbar** | The standalone Go sandbox daemon in `sandbar/`, driven over HTTP. Docker backend; Firecracker backend experimental |
| **Device helper** | `prismoid device helper`, the one local-mode process that touches the Mac. It runs every agent command under a Seatbelt (`sandbox-exec`) profile and judges every path and network request itself |
| **Egress allowlist** | The hosts a frame may reach, per step, enforced by an egress proxy outside the frame |
| **Screen** | A live view of a browser or desktop Machine in the web app |
| **Control lease** | A person taking over a Machine's screen; the agent waits on a `control` gate until it is handed back |
| **Ticket** | A short-lived (30 s), single-use, signed credential for opening a screen or display stream, so the long-lived token never rides a URL |

## Artifacts

| Term | Meaning |
|---|---|
| **Publish** | The only way an artifact comes to exist: an agent calls `frame.publish` with a path, type and title; the frame copies the bytes and appends a manifest entry |
| **Manifest** | The frame's list of what was published, with content hashes |
| **Harvest** | Reading the manifest after a step, storing the bytes content-addressed, inserting artifact rows and emitting events. `operations/artifacts.harvest` |
| **Artifact** | A published output: document, spreadsheet, deck, collection, page, code, image. Edits make revisions |
| **Rendition / preview** | A rendered view of an artifact (a PDF of a document, page images) produced in the frame |

## People and access

| Term | Meaning |
|---|---|
| **Workspace** | The tenant. Every row belongs to one; nothing references across workspaces. `db/models/tenancy.py` classifies every table |
| **Project** | A shared folder inside a workspace, with an owner and members (editor or viewer). Members see every task in it |
| **Actor** | The principal an action is attributed to: a human user, or an agent or API key acting for one |
| **User** | The human behind an actor: email, profile, password, sessions |
| **Session token / API key** | Opaque random tokens, stored only as hashes. Sessions belong to users (`cs_` prefix); API keys to actors (`ck_`) |
| **Platform admin** | An operator who can use the admin console. Created on the server with `prismoid admin bootstrap` (or by `prismoid up`'s first run), never through the web |
| **Connector** | A user's link to a remote MCP server (a service's tools), with encrypted credentials and a per-tool policy (allow, ask, off). `ai/tools/connectors.py` |
| **MCP** | The Model Context Protocol, the standard way a service exposes tools to AI agents |
| **Bot** | A user-authored agent stored as data. It compiles to an `AgentDef` per task and never replaces a built-in agent |
| **Conversation** | A chat with a bot. A message can become a task |
| **Channel** | A way to reach a bot from outside the app (a webhook, Telegram), authenticated by its own token |
| **Routine** | Standing work: a saved task request plus a schedule or trigger. Every fire creates an ordinary task with a budget, and every skipped fire is recorded with its reason |

## Running Prismoid

| Term | Meaning |
|---|---|
| **Dev stack** | Postgres and Silo (the S3 store) in Docker (`just up`), with the API, worker and web running on the host (`just dev`) |
| **Local mode** | `prismoid up` on macOS: embedded Postgres over a Unix socket, files on disk, the Mac as the Machine. `PRISMOID_ENV=local`, `backend/prismoid/local/` |
| **Blob door** | In local mode, the signed, short-lived URL the API serves stored files from, in place of an S3 presigned URL |
| **Operator console** | The admin web app, built separately (`APP_TARGET=admin`) and served from its own origin |
| **Folder rules** | The `CLAUDE.md` file in each important folder: what it is for, what to do and never do, and which checks enforce it |
| **Laws** | Tests in `backend/tests/laws/` that each enforce one standing rule over the whole codebase |
| **Quality ratchet** | `just quality`: size and complexity caps; known offenders may only shrink |
| **Decision record (ADR)** | A numbered document in `docs/adr/` recording a design decision and its reasons. Index: [docs/adr/README.md](adr/README.md) |
