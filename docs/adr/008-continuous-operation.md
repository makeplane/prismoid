# ADR-008 — Retire the Phase-1 build protocol; operate continuously

*Status: accepted · July 2026 · closes the m-stage era*

## Context

The first build's definition of done was met at tag `m9` ("build complete"). The protocol that
built Phase 1 defined green as *replayable history*: every micro dropped its terminal validation
into `tools/validate/`, and rule 4 made them accumulate — `just validate m0 … m9` re-proved all
of history on demand. That was the right harness for bootstrapping a production system micro by
micro, with a fresh session at every step. Post-m9 it inverted into a tax:

- 56 scripts, 2,779 LOC; ~30 made real model calls; poll ceilings reached ~16 minutes for a
  single script (M2.4). A full replay cost an hour-plus of wall clock and real provider spend —
  when everything was green.
- History's proofs had to be *maintained*: the ADR-007 reshape patched four of them just to keep
  replays honest; m10.1 was consumed by "five instrument defects, stale daemons" — repairing the
  instruments, not the product.
- CI never ran the ladder. The continuous counterparts already enforce the live invariants:
  evals nightly (golden hundred + sabotage controls), perf nightly (§15 budgets), chaos weekly
  (the kill matrix), `just check` / `just test` on demand.
- Sessions lost their anchor: the protocol's "where are we" pointer (the milestone table + the latest
  micro log + the stage tag) ended at M9, so post-m9 sessions re-derived context from 96 logs.

## Decision

**Green is a property of HEAD, not of history.**

- Gates: `just check` before every commit · `just test` before a workstream merges · drills
  (smoke / perf / load / chaos / endurance / backup-restore) on their CI cadence or on demand ·
  skill publishing still gates on green evals (§11 — product law, unchanged).
- `tools/validate/` is deleted — in this commit, after its durable invariants were promoted to
  standing tests in `c2fcff9`: API surface closed (M9.2) · migration reversibility on a scratch
  database (M0.4a) · duplicate-seq refusal (M0.4b) · no artifact↔space parent (M0.4c) · the SSE
  wire's id-rule and Last-Event-ID cursor (M0.3/M0.7) · the stored-row tamper test (M2.3).
  Already standing, so not duplicated: quote-match (`test_council_suite`), the crypto tamper
  matrix (`test_signing`), no-plaintext-at-rest (`test_auth`), versioned repricing
  (`test_prices`), the N+1 tripwire (`test_nplus1`), the Rule-8 record path (`test_ledger`;
  end-to-end Rule 8 = `just smoke`'s ledger assertion).
- The model-honesty instruments move, not die: `verify_bytes.py` and `judge_bytes.py` →
  `evals/instruments/` — point them at any document to prove the verifier still fails a
  fabrication; the nightly enforcement is the golden suite's sabotage controls.
- Operational instruments stay in `tools/`: `parse_frame.py`, `emit_event.py`, `make_ghost.py`,
  `ledger_ping.py` — wired to real paths, useful against a live system. `kill_drill.sh` is
  deleted: `chaos.sh` + `chaos.yml` are the same drill, kept.
- The micro-logs are frozen as history — no new ones, nothing moved or rewritten.
  (The perf baseline is a live CI input, not history; it now lives at
  `backend/tests/perf/baseline.json`, beside the harness that reads it.) **`STATE.md` at the repo root is the session anchor**: read first, updated when a
  workstream lands.
- Commits: conventional, any sane size; trunk `main`; tags mark releases, not stages. Red =
  stop. §14's "nothing thrown away" is honored the way it defines: removal + replacement +
  reason in one commit — this ADR is the reason, the promoted tests are the replacement, and
  tags `m0`–`m9` plus the micro-logs remain the archaeology (browse any era with
  `git worktree add /tmp/m9 m9`). (Amended 2026-11-12: the public repository starts from a fresh
  history, so the tags and the micro-logs stay in Makeplane's private development archive.)
- HANDOFF §1–§15 remain the living constitution; `CLAUDE.md` is maintained directly at the
  root. (Amended 2026-11-12: for the public repository HANDOFF dropped the first build's
  protocol, milestones, environment list, first rules file and definition of done, the sections
  after §15, and the environment list's URL-topology rule moved into §14.)
- ADR-007's validation invariant is amended in place: the reshape landed gated on `just check`
  + the full suite + byte-identical `just export-contract` + `just smoke`, not stage replays.
- (Amended 2026-11-26: evals run weekly, not nightly, for cost. A nightly of the whole golden
  hundred could spend up to 100 tasks x the default 2 USD budget every night and mostly re-measured
  an unchanged tree. Now: a seeded sample of 20 golden cases with each task capped at 0.50 USD and
  15 trivial50 rows, Mondays, skipped when nothing the evals measure changed since the last green run
  (the prompts every agent reads, the skills, the model catalog seed, the eval cases, the lockfile);
  a pull request that changes `prismoid/ai` or `skills` decides a seeded sample of 50 outcome rows
  in process (`--suite decide`, advisory); the whole hundred runs by hand before a release. The
  golden cases also moved from code to data, `evals/cases/golden.jsonl`.)

## Consequences

- A change is proven by the suite and one real run, not by re-proving nine stages. The cost of
  a structural change drops from hours (replay + instrument repair) to minutes.
- The egress *allow*-path drill is manual now (`computer dev frame-selftest --egress`, needs
  internet); the deny/no-route half is pinned by `tests/integration/test_frame.py`. The
  job-ghost sweep (M0.8b's scenario) keeps its instrument (`tools/make_ghost.py`) but has no
  standing pytest. Both are recorded in `STATE.md` as known debt.
- The frozen logs still describe commands that no longer exist (`just validate mN`); they are
  records, not runbooks, and are read as such.
