# Engineering principles

How code is written here, with short examples in this repo's idiom. The root `CLAUDE.md` says WHAT
the system must do and where each kind of thing lives; this file says HOW a good change reads. It is
the craft standard behind ADR-075: the ratchet and the laws hold what a machine can measure, and the
folder rules files and the `/review` agents hold the rest. The evidence for every principle is an
internal structure review of 2026-10-29 (six patterns and the house style) and the refactor plan
that pays the debt down; neither is published, and the principles below carry what they found.

The short version: the code did not get more complicated per line; it got **concentrated and
copied**. Every principle below exists to stop one of those two things.

## The principles

### 1. One job per file

A module serves one noun or one decision. When a file needs a `# ── section ──` banner for a
different job, it is two files. `operations/stream_tickets.py` is one resource in 104 lines;
`operations/steering.py` grew `set_budget`, `move` and `update_meta`, which are not steering, and
every feature that needed "may they edit?" imported the cycle hub to ask.

```python
# Bad: authorization lives in a verb module, so callers import steering to ask a question
from prismoid.operations.steering import may_edit
# Good: a leaf that imports only db + errors (planned)
from prismoid.operations.task_access import may_edit
```

### 2. Deep modules, and the private-helper test

A good module hides a lot behind a small surface: `jobs/durable.py` puts the whole durable engine
behind a handful of decorators; `ai/capabilities/tool_record.py` is the one tool-call record in 143 lines. The test for
a private helper: does it hide real work, or only rename a call? A pass-through adds a name and
hides nothing.

```python
# Bad: a pass-through (operations/machines/common.py today)
async def db_now(session): return await Machine.db_now(session)
# Good: one home, called directly (planned)
now = await db_now(session)  # db/session.py: the one Postgres clock
```

### 3. Leave it healthier

Touching a file is the moment to pay down a little of its debt, never to add to it. A PR may not
push a baselined offender past its number (`just quality` fails), and when it shrinks one, it runs
`just ratchet` so the baseline follows (`just quality` fails while the baseline is looser than today).
"Touch-to-fix" (the first feature that needs a hub pays for that hub's split
first) is owner decision D4, still open: a recommendation until it is decided, not a rule.

```python
# Bad: a new gate kind is one more arm in gates.resolve (complexity 26 -> 28, `just quality` fails)
elif spec.kind is GateKind.CONNECT: ...
# Good: the first commit moves the existing arms into handler rows (the baseline drops,
# `just ratchet` records it), and the feature adds its row
ACCEPT = {GateKind.INPUT: accept_input, GateKind.CONNECT: accept_connect}
```

### 4. Consistency over cleverness

The second way of doing a thing costs every reader. Copy the existing shape even when a new one
looks nicer, and if the new one really is better, change all of them in a tidy-first commit. A CLI
that is half inline and half per-file, two effect placements and three "who am I" hooks are this
rule broken.

```ts
// Bad: a third hand-written media query in a component
const wide = useSyncExternalStore(sub, () => matchMedia("(min-width: 64rem)").matches);
// Good: one hook, one home (planned)
const wide = useMediaQuery("(min-width: 64rem)");
```

### 5. Make illegal states unrepresentable

A vocabulary is a `StrEnum` declared once in `schemas/`, and the column, the field and the
parameter all use it. Parse at the edge: a serializer turns wire strings into typed values, and
nothing further in compares raw strings. `frame_profile` compared as a string 27 times and a
33-key `resume_state` dict are what happens otherwise.

```python
# Bad: the vocabulary is a regex, a Mapped[str] and a comment, each free to drift
work_mode: str = Field(pattern=r"^(ask|auto|never)$")
# Good: one enum, every layer
work_mode: WorkMode = WorkMode.AUTO  # schemas/conversation.py
```

### 6. Pure decisions apart from I/O

Read the facts, decide in a pure function, then act. The decision takes rows and `now` as
arguments, returns a frozen value or an enum, and is tested without a database. The shell holds the
lock and carries out the verdict. `operations/execution/joins.decide` and `operations/machines/reconcile.py`
over `schemas/machines.sweep_actions` are the shape.

```python
# Good: the lock holder stays thin; the law is a total function
verdict = joins.decide(spec, states, primary_state)  # -> JoinDecision.PROMOTE | SKIP | WAIT
if verdict is JoinDecision.PROMOTE:
    step.state = StepState.READY
```

### 7. Flat code: early returns, nesting at most 3

Guard clauses first, the happy path last and unindented. A fourth level of nesting is a helper
waiting to be named. The ratchet holds nesting at 3 and complexity at 15 for new code.

```python
# Bad
if task:
    if task.status is TaskStatus.RUNNING:
        if steer.epoch == task.plan_epoch: ...
# Good
if task is None or task.status is not TaskStatus.RUNNING:
    return None
if steer.epoch != task.plan_epoch:
    raise StaleSteer(...)
```

### 8. Names in the project vocabulary

Task, Step, Job, effect, Workspace, Project, Machine, frame, Capture, Inbox, publish, Bot,
Routine, Conversation: the words in the root `CLAUDE.md` mean one thing each, and code uses them. ADR-067
lists the retired names; never reintroduce one. A function is named for what it does in the domain
(`resolve_gate`, `release_parked`), not for its mechanics (`handle_data`, `process2`).

```python
# Bad: a retired name (ADR-067) and a mechanical verb
async def process_space(session, space_id): ...
# Good: the vocabulary's noun and the domain verb
async def move(session, actor, task_id, to_project_id): ...  # a task moves between Projects
```

### 9. Comments say why, and never cite review artifacts

A comment states the reason in one clause. It may cite an ADR or a HANDOFF section, which are
permanent; it never cites a ruling, a review item, a plan task or a meeting, which are archived and
rot. The ratchet counts those citations (`quality:comments.process_refs`, cap 0).

```python
# Bad: the reason is somewhere else, and that somewhere will be archived
# per ruling T10-seed-hold
# Good: the reason, here
# a fork's seed hold moves state_since but not activity, so idleness counts from last_active_at
```

### 10. No speculative code; the rule of three

Write what today's caller needs. No unused parameters "for later", no hook with one implementation,
no abstraction for one use. The second copy is a smell; the third copy gets a helper in the same
PR. The review found ideas copied up to 28 times (the event envelope) because nobody stopped at
three.

```python
# Bad: a Protocol and a factory for the one driver that will ever answer
class ThumbnailMaker(Protocol): ...
def thumbnail_maker(kind: str = "default") -> ThumbnailMaker: ...
# Good: the function today's caller needs; extract when the third caller arrives
async def thumbnail(frame: FrameDriver, path: str) -> bytes: ...
```

### 11. Errors handled deliberately

Catch the error you expect, where you can do something about it, and let the rest fly. Never turn
an outage into a different answer. A function that never raises says so in its docstring
(`operations/execution/proof.py` does).

```python
# Bad: a database outage becomes "not found" (api/stream.py today)
except Exception:
    await ws.close(4404)
# Good: name the miss; everything else propagates
except NotFound:  # prismoid/errors.py
    await ws.close(4404)
```

### 12. Tidy first, in its own commit

A move, a rename or an extraction lands in a commit that changes no behaviour; the behaviour
change follows in the next one. A reviewer can then read the move as a move
and the change as a change. When a move retargets a name, the test patches move in the same commit
(`tests/laws/test_law_patch_where_defined.py` catches a patch left on a re-export).

```text
Bad:  feat(gates): approval timeouts        (moves resolve into gates/resolve.py AND changes it)
Good: refactor(gates): resolve into gates/resolve.py, patches retargeted   (no behaviour change)
      feat(gates): approval timeouts                                        (the change, alone)
```

## A new kind is a row, not a branch

The most expensive pattern in the review is **accretion into hubs**.
`execute_step` was 622 lines with complexity 63 in September 2026. Steering, pause, budget,
browser takeover, pacing and the screen each arrived as one more `isinstance` arm in the ladder, one
more Deferral class, one more `_park_on_*`, one more `_hydrate_resume` branch. Each change was
small and reasonable on its own. Six features later the function was 965 lines at complexity 99,
the file was 4,949 lines, and tests imported 27 private names from it because nothing inside was
reachable any other way.

The rule: when you add a new **kind** (a park, a gate, a lane, a frame driver, a tool family, a
task-page surface, an artifact renderer), it is **one table row plus one module**, never a new
branch in an existing function.

```python
# Bad: the eighth arm in a 965-line function
elif isinstance(outcome, ControlDeferral):
    ...  # 40 lines that know about leases, gates and resume_state
# Good: a channel row the ladder walks (planned)
PARK_CHANNELS = {..., ControlDeferral: control_park}  # its own module, its own tests
```

When the table does not exist yet, building it is the first, tidy-first commit of the feature.
The places that already work this way are the model to copy: `web/app/components/artifact/
renderers/registry.ts` (a kind is one file plus one line), `web/app/components/tasks/gate-dock.tsx`
(one dispatch site for a closed set), `ai/agents/all.py` (an agent is one file, one prompt, one
import).

## Write it like this

The exemplars the structure review found. When in doubt, open one and copy its shape.

| Area | Exemplar | Why |
|---|---|---|
| Durable seam | `backend/prismoid/jobs/durable.py` | Deep module, small named surface, contract stated in the docstring |
| Pure core / shell | `backend/prismoid/operations/machines/reconcile.py` + `schemas/machines.sweep_actions` | Read facts, decide in a pure function, dispatch with deterministic ids |
| One resource | `backend/prismoid/operations/stream_tickets.py` | 104 lines, invariant at the top, caller passes the session |
| Execution module | `operations/execution/settle.py`, `proof.py` | One job, frozen result value, extraction convention in the docstring |
| Tool record | `backend/prismoid/ai/capabilities/tool_record.py` | 143 lines, one choke point, persistence behind a Protocol |
| Pure decision | `backend/prismoid/operations/chat_verdict.py`, `ai/gateway/resolve.py` | Pure door + pure plan, frozen values |
| API view | `backend/prismoid/api/endpoints/screen.py`, `api/hub.py` | auth, one call, serialize; deep hub behind three verbs |
| Values | `backend/prismoid/schemas/routines.py` | StrEnum per vocabulary, `now` passed in, library fenced here |
| Web | `hooks/use-task-stream.ts` + `lib/stream-frame.ts`; `components/tasks/gate-dock.tsx`; `routes/home/route.tsx` | Thin shell over a pure tested fold; one dispatch site per closed set |
| Go | `sandbar/internal/ticket/ticket.go`, `internal/relay/relay.go` | Two-verb surface, injected time and env, typed sentinels |
| Tests | `tests/unit/operations/machines/test_machine_decisions.py`, `test_screen_relay.py`, `tests/laws/test_tenancy_map.py` | No I/O for decisions; everything injected; laws over real metadata |

## How the guardrails fit together

Three layers, cheapest first; each catches what the one below cannot (ADR-075).

1. **The ratchet** (`just quality`, part of `just check`, blocking). `backend/tools/quality.py`
   measures every symbol against the caps: Python complexity 15, 80 statements, 3 nested blocks, 5
   positional arguments, 800-line files, 20-line endpoints; web complexity 15, depth 3, 300-line
   functions, 800-line files; Go 600-line files, 60-line functions; process-artifact citations 0
   per file; test private-access ignores (a bare `# pyright: ignore`, any `# type: ignore` and a
   file mode below strict included) may not grow. Today's offenders sit in
   `backend/tools/quality_baseline.json` and may only shrink; `just ratchet` lowers the baseline
   and re-keys moved symbols, `just quality-report` prints the before/after table a PR quotes. A
   raised baseline value is a review finding, never a fix.
   Renaming an over-cap file: `git mv` it (stage the rename or commit it), then `just ratchet`; the
   ratchet pairs the old path with the new one through git's renames, which a plain `mv` hides.
2. **The laws** (`backend/tests/laws/`, run by `just test-unit`, blocking). One file per standing
   rule; each names its rule in the docstring and derives its population from code, so a new
   table, route or agent is covered without editing the test. Literal greps live in
   `backend/tools/audit.sh` (`just audit`), import directions in `backend/.importlinter`
   (`just layers`), web colour and shape rules in the `lint:*` scripts of `web/package.json`.
3. **The rules files and the reviewer** (advisory). Each important folder has a `CLAUDE.md` (with
   an `AGENTS.md` symlink): what the folder is for, its Do and Never rules, what a machine already
   checks, the exemplars, and the judgement rules a reviewer applies there. Coding agents read it
   when they touch the folder. `/review` diffs a branch against `main`, sends each area's files to
   its own reviewer with the rules read **from `main`** (a branch cannot loosen its own review),
   and has a verifier confirm every finding before it is reported. It skips style and anything a
   check already catches; a rule finding quotes the rule. Trust boundary: `/review` runs the
   branch's justfile, tools and oxlint config (`just quality-report`, `just check`) and loads its
   own prompts from the working tree, so run it only on a branch you would run `just check` on.

The order matters when you add a rule: if a machine can decide it, it becomes a law, a grep or a
contract, and the folder file says "checked by machine" so the reviewer skips it. Only judgement
stays in prose.

Onboarding notes:

- `just check` needs `web/node_modules` (`pnpm install` at the repo root), because `quality` runs oxlint.
- `quality` measures the files on disk, so delete untracked scratch files over a cap before it runs.
- A ruff or oxlint upgrade that changes the counting re-baselines with
  `uv run python tools/quality.py ratchet --init --force` (from `backend/`) in its own commit.

## Changing a rule

Rules change in the open, never by drift.

- A standing rule (root `CLAUDE.md`, a folder `CLAUDE.md`, this file) changes through an ADR, and
  the ADR's PR edits every rules file it touches in the same PR. The root `CLAUDE.md` wins on
  conflict; a folder file narrows it and never contradicts it.
- A cap changes in `quality.CAPS` with an ADR amendment. Caps tighten after the planned
  refactors' second wave; a cap is not raised to admit one change, the change splits instead.
- A new law ships with its rule cited in its docstring and a test that proves it bites
  (`backend/tests/laws/README.md`).
- A rules file may cite only ADRs, laws, `just` recipes and quality metrics that exist;
  `tests/laws/test_law_rules_files.py` fails the build otherwise.
- A PR that changes a documented surface amends the document in the same PR (`web/DESIGN.md` for
  the web, the folder `CLAUDE.md` for a folder).
