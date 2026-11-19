# ADR-001 — Layer-first architecture, and the two seams it forces

**Status:** accepted (M0.1) · **Context:** HANDOFF §1, §2, §7, §14

## Decision

The layering law of §7 is enforced mechanically by `.importlinter` (`just layers`), not by
review. Higher layers may import lower ones; siblings may not import each other:

```
runner / api / jobs / operations / ai : features / db / schemas : conf
engine:  agents : council / deps / gateway : frames : skills : tools
```

plus two `forbidden` contracts: `api ↛ ai` (Rule 3) and `ai ↛ db|operations|jobs|api` (Rule 6).

Writing this contract down before any code exists forces two seams into the open. Both are
recorded here so they are never relitigated.

## Seam 1 — how an operation enqueues a job without importing `jobs`

`jobs` sits **above** `operations` (job bodies compose operations through `jobs/effects.py`),
and Rule 4 says operations enqueue after commit. Taken together those two facts would make
`operations → jobs` an upward import — a layering violation.

Dependency inversion at exactly one seam: `operations/dispatch.py` owns a small `Dispatcher`
Protocol (`enqueue(job, key, **kwargs)`, `send(key, payload, topic)`) and a module-level
setter. `jobs/` implements it over DBOS queues, and `runner/` (the top layer, which legitimately
imports both) wires the real dispatcher at process boot — in **both** the API and the worker
process, because the API enqueues and the worker executes.

Consequence: `operations` stays testable with a fake dispatcher and the contract stays true.
Cost: one indirection, ~30 lines. Accepted.

## Seam 2 — `agents` and `council` are siblings, and that is the point

The sketch lists `agents : council` as *independent* siblings, which is only satisfiable if
council never imports agents. It is, and the constraint is load-bearing:

- **`ai/agents/`** — the LLM callers. One `AgentDef` per file, `base.build()` the only
  constructor (§7).
- **`ai/council/`** — the *deterministic* policy and pipeline logic: plan-shape validation,
  budget math (bursar), Ed25519 signing, the verifier pipeline, the council plan template.
  It contains no model calls and constructs no agents.
- **`operations/`** — the only composer. It builds the agent (`ai.agents.build("judge", deps)`)
  **and** runs the pipeline (`ai.council.verifier.pipeline.run(judge, …)`), handing one to the
  other.

That is the law of §1 — *views never touch agents; agents never contain business logic;
operations compose both* — extended one level down into the engine. It also keeps the
deterministic half of the engine (signing, cross-footing, quote-matching) unit-testable with
no model in the loop, which is what makes verification cheap to trust.

## Alternatives rejected

- **Let `operations` import `jobs` and relax the contract.** Rejected: the contract is the
  only thing that keeps the enqueue-after-commit rule (Rule 4) from quietly becoming
  enqueue-inside-transaction, and a relaxed layer never re-tightens.
- **Put the council agents in `council/`.** Rejected: it makes `council → agents` and collapses
  the deterministic/stochastic split that makes the verifier testable.
