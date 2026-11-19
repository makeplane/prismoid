# operations/execution - folder rules
Scope: `backend/prismoid/operations/execution/`. Read with ../CLAUDE.md (operations' rules apply here and
are not repeated), the root CLAUDE.md (it wins on conflict) and docs/engineering.md.

## What this folder is for
The step ladder: `execute_step` walks one Step from entry to settle, `_attempt` holds a frame for
one model run, and the side modules each do one part of that (`settle.py` reads a guarded run's
ending, `proof.py` checks it, `answers.py` answers a park's calls, `mounts.py`, `taps.py`,
`inbox.py`). `_attempt` is a pipeline of stage modules - `acquire.py`, `equip.py` (with `ports.py`), `run.py`, `classify.py`,
`publish.py`, `harvest.py` - and returns one `AttemptResult` (`deferrals.py`): `Produced`, or the
one `Deferral` its park saves. A new stage or park kind extends that union. `execute_step` is a
short driver: `handlers/entry.py` triages the door, and each rung dispatches to one handler per
outcome (`handlers/`: rung, deferral - a table of park kinds, failure, fence, expand, published,
produce) over the `Ladder` in `handlers/state.py`; the helpers they share live in `parks.py`,
`wake.py` (its `CHANNELS` table maps a gate's resolution to the resume), `giveback.py`,
`steers.py` and `finish.py`. The express rung (`express_rung.py`) is `_attempt`'s frameless
sibling: one run of the turn's express step, the Lead's (a stored answerer step runs as the Lead); `lead_rung.py` equips a
Lead run (its facts, roster and turn note), parks its ask on the question gate and hands every other
ending to `operations/turn/commit.py`. A new park kind is a row in `deferral.ARMS` and `wake.CHANNELS`
plus its handler. It is NOT where a
Machine's lifecycle is driven (`operations/machines/`), where gates are opened or resolved
(`gates.py`), or where plans are drafted (`planning.py`).

## Do
- New code lands in a side module with one job, never in `__init__.py` (939 lines, baselined; it only
  shrinks). `__init__` re-exports a moved name under its old spelling when
  callers need it, as `settle.py` did.
- A new park, gate or lane kind is a table row plus a module, never a new `isinstance` arm in
  `execute_step`, a new Deferral class copying the same fields, or a new `_hydrate_resume`
  branch. When the table does not exist yet, the feature's first commit builds it.
- A decision is a pure, public function in its own module (rows and `now` in, a frozen value or
  an enum out), so a test imports it instead of a private of a baselined hub.
- Keep durable-replay strings frozen: the `ASKING` / `PACING` returns of `execute_step` and every
  `@durable.job` / `@durable.effect` name are journalled (ADR-069, ADR-027). A typed return
  later needs a decode shim that accepts both shapes.
- The frame-holding `try/finally` of an attempt stays inside one function.
- A helper that gives a Machine back takes ids, never a live row, and runs after the step
  session has committed.
- Steering follows ADR-064's safe points: a call chosen before a steer is held, never run; an
  action or navigate turn, or a step whose attempt called a connector or drove the browser, is
  never re-run.
- An indented `from prismoid.` import carries its reason on the line (`# cycle:` or `# heavy:`).

## Never
- Call `bind`, `freeze`, `thaw`, `archive`, `unpack`, `destroy`, `suspend` or `resume` on a frame.
- Add a key to `resume_state` outside the park code (`schemas/park.py` holds its shapes).
- Compare `frame_profile` to a string literal again.
- Write `TaskStatusChanged` or a `PlanEdit` row plus event by hand a fourth time; the ceremony gets
  one writer.
- Rename an effect, a job, or a sentinel string a replayed workflow may see.

## Checked by machine
- `backend/tools/audit.sh`: "no frame lifecycle verbs in operations/execution" (ADR-063).
- `tests/laws/test_law_patch_where_defined.py`: a test that patches a re-exported name nothing
  reads fails, so a move cannot silently disarm a patch.
- `quality:py.complexity`, `quality:py.statements`, `quality:py.nesting`, `quality:py.positional`,
  `quality:py.file_lines`. Baselined here: `__init__.py` (939 lines) and `_attempt` (nesting 4,
  7 positional parameters). They may only shrink.
- `quality:comments.process_refs`: no comment here carries a ledger citation; new ones carry none.

## Write it like this
- `settle.py`: one job (read a guarded run's typed ending), a frozen result value (`Settled`),
  every helper hides real work.
- `proof.py`: a near-pure check with `Final` constants and a `Protocol` for the frame it reads;
  it never raises, and says so.
- `joins.py` with `finish.promote_dependents`: the decision is a total function
  returning a `StrEnum`; the lock holder only carries out the verdict.
- `answers.py`: one concern in its own module, pure: a park's answers from its record, its replayed
  turn and the gate's resolution.

## Review focus
- Any diff that grows `execute_step` or `_attempt`: is it a new arm that should be a row? Quote
  "a new kind is one table row plus one module".
- A moved function whose test patches did not move in the same commit.
- A lock or transaction that changed shape: what commits where, which row is held while a
  Machine is handed back, whether the attempt's `try/finally` still covers the frame.
- A replay hazard: a renamed effect, a changed sentinel, a new effect inserted between existing
  ones in a workflow body.
- A steer path that can run a call chosen before the steer, or re-run a side-effecting step.
- Moves and logic changes in the same commit.
