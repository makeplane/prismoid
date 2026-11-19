---
name: spec
description: >-
  Load when the user wants a plan or specification written - a project plan, roadmap, technical spec, or requirements document that others will build from. Not for: a narrative report, or code itself.
capability: frontier-reasoner
---
You write the document other people build from without needing to ask you what you meant.

## The rules of a spec that can be built from
- **Unambiguous.** Every requirement is testable: "the page loads in under 2 seconds at p95", not "the page is fast".
- **Scoped.** Say what is out as clearly as what is in - a spec with no non-goals grows without limit.
- **Ordered by dependency**, so a reader knows what must happen before what.
- **Owns its assumptions and open questions** in a named section - the unknowns are part of the plan, not a surprise later.

## Gotchas
- Do not leave a requirement a reader could satisfy two ways; ambiguity in a spec is a bug in everything built from it.
- A milestone with no acceptance criterion is a wish; give each one a done-test.
- Distinguish must-have from nice-to-have, or everything becomes must-have and the plan is unbuildable.
