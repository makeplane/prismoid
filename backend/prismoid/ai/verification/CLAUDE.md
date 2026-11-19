# ai/verification - folder rules
Scope: `backend/prismoid/ai/verification/`. Narrows ../CLAUDE.md and the root CLAUDE.md; never contradicts them.

## Purpose
Checking a deliverable, never producing one. It takes values in; `operations/verification.py`,
`operations/grounding.py` and `operations/qa.py` own the rows, the model calls, the jobs and the frames'
lifetime. The judge itself is the agent `ai/agents/judge.py`, seated by `gateway/resolve.judge_seats`.

## What is here
- The receipt, after delivery: `pipeline.py` ships a suite (`suites/<kind>.py`) into a fresh frame and
  reads its `Check`s back, renders a paged file's pages for the judge (`render_pages`), and holds the
  verdict law (`verdict_of`, `final_verdict`). `grounding.py` decides in code whether each claim the claim
  reader lists holds in the sources it cites, and owns `judge_fence`, the fence every judge input wears.
  `change.py` reads a device change's published tar in the worker - the one suite with no frame.
  `signing.py` signs the report (Ed25519).
- The long-form QA gate, before delivery (ADR-026): `qa_precheck.py` runs in a fresh frame, and
  `qa_outcome.py` is the pure outcome law (pass, revise, or ship with notes).

## Where a thing goes
- A check for a new kind: a suite script in `suites/`, registered in `pipeline.SCRIPTS` (and `COMPANIONS`
  when it loads a sibling).
- Not here: a verification row, job or frame lifetime -> `operations/`; the judge's prompt -> `ai/agents/`.

## Rules
- Write each suite and `qa_precheck.py` as a script that runs alone in a frame: JSON job on stdin, JSON
  rows on stdout, no `prismoid.*` import. A parser pinned in the frame image is fine, imported lazily where
  the worker may load the module.
- Read a deliverable's words only through `suites/extract.py`, loaded by exact path; a suite may still
  read the structure it checks itself (formulas, shapes, a test report).
- Run a frame suite only through `pipeline.run_suite()`: suite files owner=system and 0555, an absolute
  interpreter, a fresh frame with no network - a verifier inside the frame it checks can be lied to.
- Keep the decisions pure (`qa_outcome.outcome`, `verdict_of`/`final_verdict`, `grounding.ground`), so
  "would this pass?" is a unit test.
- Fence everything the judge reads (sources, extracts, a change's evidence) with `judge_fence` and a fresh
  nonce.
- Let a deterministic FAIL stand as a floor and a judge FAIL sink; a judge PASS lifts only UNVERIFIED, and
  only when the judge read the content - a hash or a skip proves nothing about content.
- Raise `SuiteNotBuilt` for a kind with no suite, and record a crashed suite as a structural FAIL, so "our
  verifier broke" never reads as "your file is wrong", or as a pass.
- Keep `qa:` rows out of a `VerificationReport`: "unverified" belongs to the receipt; QA ships notes.
- Recompute validity over the stored report against our key, never the key in the row - a key that
  travels with a document is a label.
- Seat the judge furthest from the maker this host can build (another family, then the same family, then
  itself), and name which on the receipt.
- Never let a receipt block or fail the thread. The fleet (ADR-010) and the long-form gate review before
  delivery and may send work back once; an infrastructure failure there ships with a visible note.
- Never execute model-written bytes in the worker: `change.py` only reads them, every read is bounded, and
  no content reaches a check's `detail`.
- Never import a suite from application code (tests may).

## Checked by machine
- `.importlinter`: `verification` sits under `plugins` and over `context`.

## Verify
`uv run pytest tests/unit/ai/verification -q`; with a frame driver,
`uv run pytest tests/integration/verification -q` (`test_verification_tamper.py`, `test_planted_defects.py`);
one artifact by hand: `prismoid artifacts verify <id>`.
