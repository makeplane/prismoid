# backend/evals

Evals measure the AI layer: what a person gets for an ask, and what it cost. Tests (`backend/tests`)
prove the code does what it says; evals score how well the agents decide and deliver, so a prompt,
an agent or a model change is judged by numbers, not by one run that looked right.

## Layout

| Path | What it holds |
|---|---|
| `run.py` | The CLI (`python -m evals.run`, `just evals`): every suite starts here. `SUITES` registers the dataset suites. |
| `schema.py` | `OutcomeRow`: the dataset row, its checks and limits. Every vocabulary is an enum here. |
| `trace.py` | `PrismoidTrace`: one turn reduced to the facts every evaluator reads. |
| `metrics.py`, `report.py` | The numbers a report quotes, and the one renderer (`python -m evals.report RUN.json`). |
| `datasets/` | The public rows, one JSONL file per set (see its README). Data only. |
| `cases/` | Case lists that live in code: the golden hundred, the per-bot and per-skill golden lanes, the Lead's parity book. |
| `suites/` | One capability's rows scored on what the person got: routing, bot selection, research, documents, web. |
| `runners/` | One row in, one `PrismoidTrace` out: `live` (a real task), `conversation` (a bot chat), `decide` (one agent in process), `plain_loop` (the comparison row); `summary` writes the report. |
| `evaluators/` | Scoring: the outcome verdict, the row's checks, its budget, the four golden scores. |
| `instruments/` | One-off scripts that run the verifier or the judge over two local files. |
| `fixtures/defects/` | A frame-resident script that builds planted-defect deliverables (read as bytes by a test). |

Rule: case data is kept apart from grading. A row says what the person should get, never which
rule, lane or agent should fire, so the datasets survive every rewrite of the code that decides.

## Offline (free, no stack, no model)

Run these before and after any change here; none of them calls a model or the API.

```bash
cd backend
uv run python -m evals.run --help
uv run python -m evals.run --dry-run                  # every dataset suite: rows it would run, none submitted
uv run python -m evals.run --dry-run --suite outcomes # one suite
uv run python -m evals.cases.lead_parity              # the Lead's parity book, scripted model, prints the tally
uv run pytest tests/unit/evals tests/laws -q          # the eval code's own tests and the dataset laws
```

`tests/laws/test_law_no_dataset_phrases_in_prompts.py` fails when a prompt or tool description
repeats a phrase from a dataset ask. Fix the prompt by stating the rule in its own words; never edit
a row to make it pass.

## Live (costs money, needs a stack)

A live run submits real tasks to a running API and worker (or, for `decide`, `plain-loop`,
`routing` and `bot-selection`, calls the configured models in process). Log in first
(`uv run prismoid login`); the harness reads the stored token.

```bash
uv run python -m evals.run --suite golden --limit 20 --report report.json
uv run python -m evals.run --suite trivial50          # a dataset suite; see --dry-run for the list
uv run python -m evals.run --suite outcomes --trials 3 --baseline old.json --gate
uv run python -m evals.run --suite routing            # also: bot-selection, bot-golden, skill-golden
uv run python -m evals.suites.web                     # needs the fixture admin app (tests/fixtures/admin)
```

Run only the rows you need (the flags combine with AND; `--dry-run` lists the rows they select):

```bash
uv run python -m evals.run --suite outcomes --failed-in evals/reports/outcomes-<tree>.json  # rerun the failures
uv run python -m evals.run --suite trivial50 --rows T50-001                                  # one row
uv run python -m evals.run --suite outcomes --tag connectors                                 # a tag: group, category, variant or expected outcome
```

`--match TEXT` keeps rows whose id, message or label note contains TEXT (any case), and
`--sample N --seed S` draws N runnable rows the same way every time. A selection is recorded in its
report and saved beside the whole-suite one, never over it. `--baseline` compares only the rows both
runs scored and names the baseline rows this run skipped. `--gate` refuses a selection, and refuses a
selection as its baseline: a few rows cannot vouch for a suite.

Dataset suites write `evals/reports/<suite>-<tree>.json`; `--baseline` compares with an earlier
report and `--gate` exits 1 unless the gate passes. Every suite takes a budget per task (`--budget`).
The nightly workflow (`.github/workflows/evals.yml`) runs the same commands. Inside this project,
live runs go through the eval seat only, with the owner's go.

## Adding a case

- A dataset row: append one JSON line to the right `datasets/*.jsonl` file. The row is an
  `OutcomeRow` (`schema.py`): `id`, `message`, `context` (surface, turns, dials, attachments),
  `expected_outcome` and `allowed_outcomes`, limits (`max_tool_calls`, `budget`, ...), `checks`,
  and where it came from (`source`, `label_note`). A field outside the schema fails to load. Run
  the offline commands above; the dry run must count your row.
- A golden case: add a `Golden` to `CASES` in `cases/golden.py`.
- A bot's or a skill's golden case ships with the bot or skill (`golden:` in its manifest), not here.

## Adding a suite

A dataset suite is one row in `SUITES` (`run.py`): its dataset file, its kind, its timeout and
trials. A capability suite that scores turns itself also passes `scored=` (see `suites/__init__.py`).
`--dry-run` lists it once it is registered.

## Held-out rows

A sealed held-out twin of the datasets exists outside the repo. Never copy it in, read it in a test,
or tune a prompt against it; `tests/unit/evals/test_datasets.py` and the dataset law keep it out.
