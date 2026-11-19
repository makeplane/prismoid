# Contributing to Prismoid

Thanks for your interest in Prismoid. This guide covers setting up, the checks a change must pass,
how the tests are split, how to write commits and pull requests, and step-by-step recipes for the
most common extensions.

By taking part you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md). Report security
issues privately, as described in [SECURITY.md](SECURITY.md), never in a public issue.

## Setup

Install the prerequisites listed in the [README](README.md#prerequisites) (Docker, uv, Node 22 with
pnpm 12, just; Go 1.25 only if you touch `sandbar/`). Then:

```sh
just install                          # backend dependencies, all groups (uv sync --all-groups)
pnpm install --frozen-lockfile        # web dependencies, from the repo root
cp backend/.env.example backend/.env  # set one model key for real tasks
just up migrate                       # Postgres + Silo (S3) in Docker, schema, built-in skills
just cli models seed                  # the starter model catalog
just dev                              # API :8100, worker, web :5173
```

`just` with no arguments lists every recipe. Every backend recipe runs from `backend/`, so you can
call `just` from anywhere in the repo.

## The gate: `just check`

Run `just check` from the repo root before every commit. CI runs the same command. It runs:

| Step | Recipe | What it holds |
|---|---|---|
| Lint and format | `just lint` | ruff over the whole backend (`just fmt` fixes most findings) |
| Types | `just types` | pyright in strict mode |
| Import layers | `just layers` | `backend/.importlinter`: which package may import which (the engine in `prismoid/ai` never imports the database, operations, jobs or API) |
| Audit greps | `just audit` | `backend/tools/audit.sh`: rules a linter cannot express (no model names outside provider files, no URL literals outside settings, no raw `select()` outside models, ...) |
| Quality ratchet | `just quality` | Complexity, function and file length caps. New code meets the caps; known offenders listed in `backend/tools/quality_baseline.json` may only shrink |
| Unit tests | `just test-unit` | `backend/tests/unit` and `backend/tests/laws` (one law: every source file carries the copyright header) |
| Web | `just web-check` | typecheck, oxlint, the style guards, the contrast audit, vitest |

If `just quality` says a baseline entry is now smaller than recorded, run `just ratchet` to lower
it and commit the new baseline with your change.

## How the tests are split

| Suite | Command | Needs |
|---|---|---|
| Unit (`backend/tests/unit`) | `just test-unit` | Nothing: no Docker, no keys. Part of `just check` |
| Laws (`backend/tests/laws`) | `just test-unit` | Nothing. One file per standing rule; each derives its population from the code, so a new agent, route or table is covered without editing the test |
| Integration and conformance (`backend/tests/integration`, `backend/tests/conformance`) | `just test-integration`, or `just test` for everything | `just up migrate` and `just cli models seed` first, and Docker for the tests that start sandboxes. Any non-empty provider key will do: these tests use a fake model and never call a provider |
| Web (`web/app/**/*.test.ts`) | `just web-check` or `pnpm --filter web test` | Nothing |
| sandbar (`sandbar/`) | `just sandbar-check` | Go 1.25. The Docker tier skips without a Docker socket |
| Device (`test_device_chrome*`, `test_device_stop*`) | `just device-proof` | A Mac with Google Chrome and Docker. CI has no Mac, so run these by hand when you change `backend/prismoid/device/` |
| Evals and problem books | `just evals` | Real model keys; spends money. Run it when you change an agent, a prompt or a skill |
| Smoke, range, perf, load, chaos | `just smoke`, `just range`, `just perf`, ... | A running stack, a logged-in CLI and real keys |

`just test` and `just test-integration` refuse to run while `just dev` is up: the suite clones the
dev database per worker, and a running worker would race it. Run `just stop` first.

Mark a test that needs the stack with `@pytest.mark.integration`. Frame-spinning test files join
one of the `frames-*` `xdist_group` lanes, so sandbox-heavy files do not all start at once.

## Folder rules (`CLAUDE.md` files)

The root [`CLAUDE.md`](CLAUDE.md) and a `CLAUDE.md` in each important folder (`backend/prismoid/ai/`,
`backend/prismoid/api/`, `backend/prismoid/db/`, `backend/prismoid/jobs/`, `backend/prismoid/operations/`,
`web/`, `sandbar/`, ...) hold the standing rules for that part of the code: what the folder is
for, what to do and never do, which checks enforce it, and a few files that show the style. Each
`AGENTS.md` beside them points to the same file, so AI coding tools that read either name pick the
rules up automatically.

They are written tersely, for AI tools first, but they are equally the fastest way for a person to
learn a folder's conventions. Read the one for each folder you change before you change it. The
longer reasoning behind the rules is in [docs/engineering.md](docs/engineering.md) (the craft
standard reviews use) and the decision records in [docs/adr/](docs/adr/README.md). The root file
uses some internal shorthand: "ADR-n" is decision record number n, and "HANDOFF §n" is a section of
`HANDOFF.md`, the original build specification.

A few rules you will meet everywhere:

- **Three data homes.** Database rows live in `backend/prismoid/db/models`, domain values in
  `backend/prismoid/schemas`, request and response shapes in `backend/prismoid/api/serializers`
  (`*In` for requests, `*Out` for responses).
- **Thin endpoints.** An endpoint authenticates, validates, makes one call into
  `backend/prismoid/operations`, and serializes. Operations own transactions and enqueue jobs only after
  the commit.
- **The engine is persistence-free.** `backend/prismoid/ai` takes values in and hands values back; it
  never imports the database, operations, jobs or API.
- **Migrations are Alembic only** (`just revision "message"`), and the event, plan-edit and usage
  tables are append-only.
- **A new dependency needs a decision record** in `docs/adr/` and a row in the dependency table in
  `HANDOFF.md` §3.

## Commits and pull requests

- Use [Conventional Commits](https://www.conventionalcommits.org/): `feat(scope): ...`,
  `fix(scope): ...`, `refactor(scope): ...`, `docs: ...`, `test: ...`, `ci: ...`. Any sane size;
  keep unrelated changes apart.
- Branch from `main` and open the pull request against `main`. Pull requests are squash-merged:
  the title becomes the single commit on `main`, and release-please builds the CHANGELOG from
  those titles, so the title is a Conventional Commit with nothing before the type, such as
  `feat(web): stream tickets`. No ticket or tracker id is needed.
- Fill in the [pull request template](.github/pull_request_template.md). "Where to look" (the order
  to read the diff in) and "Proof" (what you ran) matter most.
- Prove a user-visible change with one real run (`just smoke`, or the relevant CLI flow). If you
  changed an agent, a prompt or a skill, include `just evals` in the proof.
- If you use [Claude Code](https://claude.com/claude-code), `/review` (or `just review`) runs the
  repository's own automated reviewer against your branch. It is optional.
- Nothing is thrown away silently: a replacement removes the old code, adds the new code and says
  why, in the same commit.

## Recipes

Each recipe names the files to touch and the check that holds the rule.

### Add a model

A model is data, never code. With the stack up and a platform admin login
(`just cli admin bootstrap --email you@example.com` makes one):

```sh
just cli models classes                    # the model classes a model can serve
just cli models add anthropic:<model-id> --classes frontier-reasoner,judge \
  --price input_token=3/M --price output_token=15/M --context 200000 --evaluate --enable
just cli models doctor                     # what each enabled model is missing (a key, a price)
```

New models are disabled by default: `--evaluate` scores them against a smoke suite and `--enable`
routes to them. To change the starter set every fresh install gets, edit
`backend/prismoid/runner/default_models.yaml` (prices are per single token there) and run
`just cli models seed`. Check: `just audit` fails if a model name appears in code outside the
provider files.

### Add a model provider

There are three tiers (the docstring of `backend/prismoid/ai/gateway/providers.py` explains them):

1. **A new model from a provider Prismoid already speaks:** only the recipe above.
2. **A provider with an OpenAI-compatible API** (DeepSeek, xAI, Ollama, vLLM, ...): no code. Add
   models with `--protocol openai_compatible --base-url <server URL>`, and put the key in
   `PRISMOID_GATEWAY_<PROVIDER>_API_KEY` (a local server may need none).
3. **A genuinely new SDK:** add a value to `Protocol` in `backend/prismoid/schemas/model_class.py`, one
   constructor function in `backend/prismoid/ai/gateway/providers.py` registered in `CONSTRUCTORS`, and
   a decision record plus a `HANDOFF.md` §3 row for the new dependency. Every call must emit a
   `UsageEvent` (native units plus `cost_usd`) through the gateway's meter. Tests:
   `backend/tests/unit/ai/gateway/test_gateway.py`; `just audit` keeps model strings inside `providers/`.

A provider that is not a chat model (images, speech) is a tool, not a gateway entry: see
`backend/prismoid/ai/tools/providers/images.py`.

### Add an agent

An agent is a role, not a model. Three changes, nothing else:

1. `backend/prismoid/ai/agents/<role>.py`: a docstring that says why the role exists, and one
   `register(AgentDef(...))` with a `ModelClass`, a description that says what it is for and
   what it is not for, a prompt, and the smallest named tool grant. `namer.py` is the shortest
   example.
2. `backend/prismoid/ai/agents/prompts/<role>.md`: the prompt. The shared charter is prepended for you;
   never copy shared guidance into it.
3. One import line in `backend/prismoid/ai/agents/all.py`.

Check: `backend/tests/laws/test_agent_registry.py` covers the new role automatically (a role not a
model, says what it is not for, has a prompt, declares how a published artifact is checked,
grants are named). Run `just evals` and include it in the proof.

### Add a tool family

An agent holds only the tools it is granted: an ungranted tool is never mounted. Every call passes the
`tool_record` capability (`backend/prismoid/ai/capabilities/tool_record.py`), which journals one
`tool.called` row per call, with argument keys but never values.

1. `backend/prismoid/ai/tools/<family>.py`: a function that returns a pydantic-ai `FunctionToolset`.
   A row that depends on how the call went (a refusal returned as text, which engine served) is
   said through `backend/prismoid/ai/tools/call_rows.py` (`say`). `routines.py` is a small example;
   `fetch.py` shows a network tool whose refusals come back as journaled, instructive results
   instead of exceptions.
2. Name each tool once: a `CatalogTool` row (its journal name and `SideEffect`) in
   `backend/prismoid/ai/tools/catalog.py` and a `label_for` branch in `backend/prismoid/ai/tools/labels.py`,
   so the journal and the live progress label agree and never show a content argument.
3. Mount it as one function in `backend/prismoid/ai/agents/mounts.py` (as `search_tools` is) and call it
   from `build()` in `backend/prismoid/ai/agents/base.py` only when the agent holds the grant.
4. Grant the tool by name in the `AgentDef.tools` of the agents that need it.

Checks: `backend/tests/unit/ai/tools/test_tool_record.py` and `test_labels.py` for the record and the labels,
`tests/laws/test_law_tool_catalog.py` for the catalog,
`tests/laws/test_agent_registry.py` for named grants, `just layers` for the engine's internal
layering, and `just quality` (a new block inside `build()` pushes it past its cap).

### Add a web search engine

Engines are configuration. Any engine already in the registry is enabled with environment
variables only:

```sh
PRISMOID_SEARCH_ENGINES=brave,searxng                # ordered fallback chain
PRISMOID_SEARCH_KEYS=brave=<key>
PRISMOID_SEARCH_URLS=searxng=http://localhost:8888   # `just searxng` runs a local SearXNG
```

To support a new engine's API, add one wire function and one `WIRES` entry (a `WireSpec` saying
whether it needs a key, a base URL or a model, and its default price per 1,000 queries) in
`backend/prismoid/ai/tools/providers/search.py`, the only file that knows engine names. Nothing else
changes: `mounts.py` builds the chain without knowing any engine's name. Tests:
`backend/tests/unit/ai/tools/test_search_tool.py`. The query itself must never reach a journal or a label.

### Add a built-in skill

A skill is guidance an agent loads for a kind of work (a report, a deck, a spreadsheet).

1. `backend/skills/<name>/SKILL.md`: YAML frontmatter with `name`, `capability` and a
   `description` that says when to load it and what it is not for, then the body. Copy the shape
   of `backend/skills/summarize/SKILL.md`.
2. `backend/skills/<name>/cases.yaml`: routing cases (`goal` and the skill it should `expect`),
   including near misses that should route elsewhere.
3. `just seed` (or `just migrate`) loads the built-ins into the database.

Checks: `backend/tests/unit/ai/skills/test_skill_format.py`, `test_skill_files.py` and
`test_skill_routing.py` in `just check`, then `just evals`. Users author their own skills through
the CLI instead: `prismoid skills pull <name>`, `prismoid skills push <name>` (a draft only its author sees),
`prismoid skills test <name>`, and `prismoid skills publish <name>`, which the server refuses with a 409
until the skill's evals are green.

## License of contributions

Prismoid is licensed under the [GNU AGPL v3.0](LICENSE) (`AGPL-3.0-only`). By opening a pull request you
agree that your contribution is licensed under the same terms.

Before your first pull request can merge, you sign the Contributor License Agreement (CLA). The CLA
assistant bot comments on the pull request with a link; sign once and its check turns green on this
and every later pull request.

Every source file opens with the notice in [`COPYRIGHT.txt`](COPYRIGHT.txt), in the file's own comment
syntax (ADR-102). After you add a file, `git add` it and run `just copyright-fix` to write the header;
`just check` and the Copyright workflow on your pull request fail on any file without it.

## Where to ask

Open a GitHub issue for bugs and feature proposals. For a larger change, open an issue to discuss
the design before you write the code.
