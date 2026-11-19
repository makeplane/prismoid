# ADR-055 · The charter: one identity, every seat

**Status:** accepted · 2026-08-30 · the owner's naming call: the system speaks as **Plane**,
an AI factory; **Computer** is the worker Plane built. ADR-045's "One" stays engineering
vocabulary for the planning front door and is never spoken to a user.

**Amended 2026-11-14 (identity lines).** The 490-word charter is gone: `prompts/_identity.md` holds three
identity lines and a short `## Rules` block (voice, assumptions, invent nothing, the em-dash ban), said
once; `AgentDef.charter` is now `AgentDef.identity`, declined by the same three seats. The product's nouns
and how it behaves moved to the `prismoid-product` skill (`backend/skills/prismoid-product/`); until skills load
on demand, the answerer's "Who you are" rule carries the nouns it answers with.

## Context

The system had a strong personality and no identity. Every agent's system prompt was its own
`prompts/<name>.md`, wired verbatim at the single seam (`ai/agents/base.build()`,
`instructions=definition.prompt`), and nothing was shared: no agent was ever told what system
it belongs to, what that system promises, or what the product's nouns mean. Asked "who are
you?", the express lane answered from model weights.

Meanwhile the identity already existed, scattered where no model reads it: the product
one-liner in `computer/__init__.py` ("a general-purpose digital worker that verifies before
it delivers"), the seven-line mental model in the first operator's guide, the orchestrator's
internal name in ADR-045, and the behavioral maxims living as code comments (the bursar's
"stop before, not apologise after"; ADR-019's "the answer is primary; verification is its
receipt"; the ask tool's "asking a human is the most expensive thing you can do"; the
ladder's "an escalation without a diagnosis is a shrug with a stack trace attached"). The
house voice was real but re-derived per prompt, and its one explicit law, the em-dash ban,
was pasted verbatim at the foot of sixteen prompt files.

The landscape shape adopted is the system-persona constitution (a shared identity block plus
behavioral dispositions ahead of role instructions, the pattern the frontier assistants use).
Backstory-style personas, mascots, and UI-chrome whimsy were considered and declined: the
existing prompts work because every line is a constraint argued from consequence, and the
web app's quiet lowercase working verbs are the right chrome for a factory.

## Decision

1. **`ai/agents/prompts/_charter.md` is the one shared identity.** Four sections: who the
   seat is (one seat of Plane, an AI factory; Computer is the machine on its floor), how
   Plane behaves (the six maxims: answer primary, stop before, asking is expensive, a failed
   attempt is not a failed task, degrade machinery never the deliverable, never silent never
   invented), the factory floor (the product vocabulary: Task, Step, Gate, Space, Capture,
   Inbox, Artifact, Routine, Bot, Connector, Skill, the ledger), and the voice (the sharp
   colleague, and the em-dash law's new single home).
2. **Prepended at the one seam.** `instructions_for(definition)` in `ai/agents/base.py`
   returns charter + role for a carrying seat, the bare role otherwise; `build()` calls it.
   The registry, `GET /v1/agents`, and the evals keep reading `definition.prompt` - the
   role's own text, never the preamble. Separated, never woven (the DELIVERY_LAW precedent,
   ADR-045). The stable prefix rides the existing prompt-cache breakpoints (ADR-012 C0), so
   the recurring cost is the cached rate, not a re-bill.
3. **`AgentDef.charter: bool = True`.** Two seats decline: the **judge** and **qa**. Their
   value is distance from the work; a preamble that makes them one of the family softens
   exactly that (the same instinct as `exclude_family`, applied to the prompt instead of the
   catalog). They keep their own copy of the style line. Bots inherit the default, so a
   user-authored agent runs with the house identity underneath and the author's voice on top.
4. **The answerer learns to say who it is.** Its prompt gains the identity rule: "I'm
   Plane", the factory sentence, product questions answered from the charter's vocabulary,
   and the honest model answer (Plane routes each job to whichever model serves the role;
   the factory, never one machine). Its local tone line is removed - the charter owns the
   voice now.
5. **The em-dash law is centralized.** The sixteen pasted copies are removed in the same
   change (removal + replacement + reason, per house rules): fourteen charter-carrying
   prompts lose the line to the charter; judge.md and qa.md keep theirs. A registry test
   enforces "the law lives once" in both directions.

## Consequences

- "Who are you?" has one answer everywhere it can be asked, and every seat knows what it is
  a seat of: what the system does when blocked (gates, one batch, safe defaults, silence
  with a policy), how it manages (the ledger, stop-before), how it fails (the ladder, the
  diagnosis), and what the product's nouns mean.
- Every charter-carrying run pays the preamble once per cache window (a few hundred tokens
  at the cached rate thereafter). Judged worth it for the classifier seats too: the
  clarifier, namer, and archivist reason better about a product they can name.
- The role prompts read leaner: identity, dispositions, and voice live in one file, and a
  prompt file is again purely what that role adds.
- Deferred, recorded honestly: the express lane's brief does not yet mount the Memory
  ledger, so "who am I?" about the *user* still answers only from the conversation. That is
  a brief-composition gap, not a charter gap, and it is the natural next piece of this work.

## Declined

- **A backstory persona** (names with lore, mascots, emoji, quirks): decoration would
  dilute prompts whose character is argued from consequence.
- **Injecting the full taxonomy** (HANDOFF/GUIDE wholesale) into every prompt: the charter
  carries the mental model; depth stays where it lives, and the answerer holds the only
  user-facing product-knowledge rule.
- **Personality in the UI chrome**: `status-copy.ts`'s quiet working verbs stand; the
  personality belongs in the agent's words.
- **Renaming "One" in code**: it remains the planning front door's engineering name
  (ADR-045); the spoken identity is Plane.
