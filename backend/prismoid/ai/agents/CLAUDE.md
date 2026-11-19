# ai/agents - folder rules
Scope: `backend/prismoid/ai/agents/`. Narrows ../CLAUDE.md and the root CLAUDE.md; never contradicts them.

## Purpose
The roster, and how an agent is built and run. Each agent is a role: one file that registers an
`AgentDef`, one prompt `prompts/<role>.md`, one import in `all.py`. Agents decide nothing about rows,
budgets or permissions - operations does, and hands the verdicts in.

## Where a thing goes
- `base.py`: `AgentDef`, `register()`, `REGISTRY`, `find()`/`get()`, and `build()`, the only constructor;
  it returns the `AgentRunner` of `run.py`, the one run loop (`runner.run(prompt)`).
- `cancellation.py`: fires a step run's `CancellationToken` when a poll sees its task cancelled.
- `grants.py`: who holds which tool, one row per agent by catalog name (`GRANTS`); `CONNECTORS` names who
  holds a task's connector tools, `TALK` a bot reply's hands, `SKILLS` who reads skills frameless.
- `mounts.py`: the `MOUNTS` table `build()` walks, one function and one row per tool family. A row that
  serves tools mounts only on a definition holding one; a row that serves none (steering, run guard,
  grant fence, Machine acquire, compaction) runs on every build and is inert without its port.
- `bot_compile.py`: a user's Bot becomes an `AgentDef` per task or per conversation, narrowed to its base
  agent's hands; it refuses a manifest through `bots/format.manifest_refusal`.
- `decision.py`: runs a built agent to its first ending or acting call (the publish gates).
  `tap.py`: the live message tap. `draft.py`: a draft read out of streaming tool
  arguments. `messages.py`: history helpers.
- `prompts/_identity.md` rides every seat that carries the identity; `prompts/_device_law.md` is appended
  on the device.
- Not here: a run hook -> `ai/capabilities/`; a pure verdict -> `ai/rules/`; text a model reads beside its
  prompt -> `ai/context/`; a tool -> `ai/tools/`.

## The roster (`all.py`)
- Step agents, the plan's vocabulary: `researcher` (search, read, cite; its answer is published), `writer`
  (prose a person receives; ends with `submit_document`), `maker` (one file built with code; the step's
  format skill names its kind), `operator` (acts in connected services, on the computer, in the browser;
  ends with `done(Outcome)`).
- The turn: `lead` ends a turn in one typed ending - a reply, a plan, an ask, a card, or, unattended, a
  delivery to the owner or a typed failure. It never parks. Its plan is held in-run to `rules/plan.py`,
  the rules the commit applies; its `replan` seat revises unstarted steps (ADR-092).
- Machinery, never a step: `judge`, `claim_extract`, `memory_extract`, `bot_lesson`, `namer`,
  `page_reader`, `summarizer`.
- A name resolves only to a registered agent (`find()`, `get()`); a stored step naming a retired agent
  fails as "no such agent".

## Rules
- Add an agent as one file, one prompt, one import - the roster law then covers it with no test edit.
- Give it a role, not a model: a `ModelClass`, a description of what it is and is NOT for, and a minimal
  named row in `grants.py` (`register()` refuses `tools=` in the agent file, so hands have one home).
- Say how a publishing agent's output is checked, so the Lead is told the truth about who publishes.
- Add a typed `AgentDef` field only for a fact read before the run's capabilities exist (by `build()`:
  model class, `endings`, `effort`, `modes`, `replan`, `makes_formats`; by operations: `cites`). Behaviour
  is a capability (ADR-090). Declare a field, never derive it from another - a derived trait drifts.
- Make a part a step agent plays in a deterministic lane (the scatter map, a gather seat) a `Mode` on its
  `AgentDef`, taken by the step's lane role, never another agent.
- Mount a new tool family as one function in `mounts.py` plus one `MOUNTS` row; the row order is the
  wrap order, so `Steering` sits before `RunGuard` before `GrantFence`.
- Lay a prompt out in `##` blocks under the snapshot test's block cap; prepend shared text, never weave it,
  and put a paragraph two prompts need in one shared fragment.
- Keep a Bot out of `REGISTRY` (a built-in wins a name collision); the only talk compile is
  `compile_bot(conversation=True)`, reached when `TaskDeps.conversation_id` is set (ADR-059).
- Offer `ask_user` as a deferred tool only to step agents that may ask; it is absent where
  `can_ask_user=False`.
- Inside a build or run, never re-resolve the definition by name or walk a built agent's capability
  tree. The run guard keeps its state per run; a resume says where to recall from on the run
  metadata (`RECALL_FROM`).

## Checked by machine
- `tests/laws/test_law_agent_construction.py`: `Agent(` only in `base.py`, a run only in `run.py`.
- `tests/laws/test_agent_registry.py`: role not model, for and not for, a prompt, minimal named grants.
- `tests/laws/test_law_rendered_tool_references.py`: a rendered text names only tools its reader holds,
  and the first request's tools equal the `grants.py` row.
- `tests/laws/test_law_plain_words.py`, `tests/laws/test_law_prompt_budget.py`,
  `tests/laws/test_law_no_dataset_phrases_in_prompts.py`.
- `tests/unit/ai/agents/test_request_snapshots.py`: each agent's first request is a reviewed
  `tests/snapshots/<agent>/request.txt`; re-record with
  `uv run python -m tests.unit.ai.agents.test_request_snapshots` and review the diff.
- `.importlinter`: `agents` is the engine's top layer.

## Verify
`uv run pytest tests/unit/ai/agents -q`; a prompt or agent change also runs `just evals`
(`--dry-run` lists suites; a chat change is `--suite conversation`), which `just check` does not.

## Copy this
- `namer.py`: a docstring saying why it exists, a prompt path, one `register(AgentDef(...))`.
- `all.py`: one import per agent and nothing else.

## Review focus
- A prompt or agent change without `just evals` and a snapshot diff in the PR.
- A trait inferred from `frame_profile`, `tools` or the agent's name instead of declared.
- `build()` or `base.py` growing a new concern.
- A guidance paragraph copied between two prompt files.
- Business logic, engine config or model-read text in an agent file: a budget, a permission, a row rule.
