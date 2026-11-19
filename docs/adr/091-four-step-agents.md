# ADR-091 · Four step agents and the judge

**Status:** accepted · 2026-11-14

## Context

Plans seat one of many built-in agents per step. Five file makers (`coder`, `modeler`, `organizer`,
`presenter`, `typesetter`) run one loop with five prompts, and `computer` and `operator` split one job
between the browser and connected services. Plans, call-log rows and forks store an agent by name
(`plan_steps.spec->>'agent'`), so a name cannot simply disappear: a stored plan must keep resuming.

## Decision

1. **The step roster is four agents and the judge.** `researcher` (search, read, cite), `writer` (prose
   the person receives), `maker` (a file built with code, its craft in format skills) and `operator`
   (acts on websites and in connected services). `judge` checks a deliverable and is never planned.
2. **A retired name is an alias, resolved in one hop.** `ALIASES` in `ai/agents/base.py` maps each
   retired name to its successor, and `canonical()` is the one resolver: a name is itself while
   registered, else its successor. A successor is never itself an alias, and a retired agent is never
   registered beside its successor (`tests/laws/test_law_agent_aliases.py`).
3. **An alias is declared before its agent retires.** The makers alias to `maker` now and resolve to
   themselves until their files are deleted; deleting the file is the whole cut-over. When `computer`
   merges into `operator`, its alias and the two names that point at it (`navigator`, `builder`) move
   to `operator` in the same change, which the law enforces.
4. **Hands are grant rows.** `ai/agents/grants.py` holds each step agent's row. A successor's row may
   exist before its agent registers; `maker`'s joins the five makers' hands, so a retired name's plan
   resumes with every tool it had. Each step agent's package narrows its row with its prompt and evals.

## Consequences

- A stored plan naming any retired agent resumes on its successor; there is no data migration.
- Bots cannot take a retired name (`operations/bots.py`); a successor name is reserved once registered.
- Aliases last until no in-flight plan names them.
- Merged machinery seats alias the same way: `drafter` and `distiller` resolve to `bot_lesson`.

## Alternatives considered

- **Rewrite stored plans to the new names.** Rejected: a migration over JSONB history for a lookup that
  one dictionary answers, and forks would still carry old names.
- **Follow alias chains.** Rejected: a chain hides a dead end until a resume walks it; one hop is checked
  whole by a law at every commit.

## Amendment 2026-11-15 - the maker lands; format skills say what they build

- `coder`, `modeler`, `organizer`, `presenter` and `typesetter` are deleted; `maker` is registered and their
  names resolve to it. Its loop is one prompt; the craft of each file moved into its format skill
  (`code`, `model`, `organize`, `deck`, `docx`, `pdf`, `page`).
- A format skill declares the file it builds in its frontmatter: `produces` (the artifact kind) and, for a
  document, `format` (`docx`, `pdf`, `html`). `ai/skills/deliverables.py` reads the built-in skills into the one
  table that replaces the kind-to-agent and format-to-skill maps.
- A maker step runs with exactly one format skill: its deliverable's, pinned when the attempt starts
  (`operations/skills.pin_format`), so a stored plan naming a retired maker resumes with its file's craft.
  That skill seats the run: its `capability` is the model class, and a collection keeps its typed answer,
  which the system publishes.
- `AgentDef.makes_formats` marks the agent whose kinds are the format skills' (`AgentDef.kinds`); a plan
  step on such an agent names its kind in `produces` when the task's deliverable does not.
- The writer finishes with `submit_document` (a title and the markdown below it); the text heuristic that
  cut narration above a document's first heading is deleted.

## Amendment 2026-11-15 - the researcher plays the scatter lane's parts

- `surveyor` and `gatherer` are deleted; their names resolve to `researcher`. A step agent may declare
  `modes` (`ai/agents/base.Mode`): a part it plays in a deterministic lane, taken when the step's lane role
  is the mode's (`TaskDeps.lane_role`, set by the step runner from the plan). The lane's expansion writes
  the role and a model never does, so no plan can choose a mode.
- A mode seats a fan-out seat: the part's typed output (which the system publishes), its model class and
  its craft blocks after the role's own; it cannot ask, holds no connector and only a seat's tools
  (`grants.SEAT_TOOLS`). The researcher's map is `ScatterResult` on the frontier class with the run guard;
  each gather seat is a `RecordSet` on the fast class. Their craft is the built-in `scatter` skill, read
  from disk so a pushed skill never changes what a lane step runs.
- A seat's record names its page by source id (`ResearchRecord.source`); the system fills the URL from the
  read and sends an unread id back (`capabilities/record_sources.py`).

## Amendment 2026-11-15 - the operator absorbs the computer, the navigator and the builder

- `computer` is deleted; `computer`, `navigator` and `builder` resolve to `operator` in one hop. The operator is
  one agent, one prompt, one model class (`computer-use`) and one ending (`done(Outcome)`), for a change made
  or an answer found by acting: in a connected service, on the task's computer, on a website.
- Where it works is the step's fact, not the agent's. `StepSpec.browser` (JSONB, defaulted, no migration) is
  set by code when the step is minted: the navigate door and the hatch's computer lane, and every planned
  operator step while computer use is on; the action door leaves it off. A step that drives the browser takes
  the browser seat (`base.browsing`): the browser image, the shell and the browser tools (grants need
  `browser`), the craft of the built-in `browse` skill after the role's own, and no connect block (grants
  `off`), since a step told to connect first never opens the page. Any other operator step holds files, the
  web and the connect block, starts on paper and never takes a Machine for a connector act.
- A stored step under a retired name drove the browser, so it resumes on the browser seat (`drives_browser`).
- A `done` with no proof holds when its summary quotes a value a connector call returned (an id, a title, a
  link), as it holds for a command's quoted output (`proof.quotes_service`).
- The site craft (finding things on a site, sign-ins, hand-overs, proof on a page) lives in `skills/browse`,
  not in a prompt; the code contract lives in `skills/code`.

## Amendment 2026-11-15 - the writer lays the fleet's table

- `chair` is deleted and resolves to `writer`. A mode is a seat that cannot ask; `Mode.quiet` (default on)
  keeps a wave's prose off the thread, and the fleet's table, a lane's one deliverable, streams its draft.
