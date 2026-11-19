<!--
Title: a Conventional Commit line, e.g. `fix(api): refuse a stale steer`. PRs are squash-merged and this title becomes the one commit on main that release-please turns into a CHANGELOG line, so nothing goes before the type. No ticket or tracker id is needed.
Reviewers read this before the diff. One line per bullet, no paragraphs. Whole body under ~40 lines; under ~12 for a small fix.
Concise means fewer words, not fewer facts: cut filler, never a risk, a gap, or a decision.
Priorities: 1) Where to look, in reading order, is what reviewers rely on most; fill it first, it is never "None". 2) The Confidence callout must match what Proof actually shows.
Write "None" where nothing applies; do not delete headings. Over ~800 changed lines: split into a stack and say so under Links.
-->

### What

<!-- Was → Now in the user's terms, then the approach in at most 4 more bullets. Not a file list. -->

- **Was:**
- **Now:**
- <!-- approach -->

### Where to look

<!-- The most important section: the order a reviewer should read the diff in. Dependency order (schema → operations → API → wiring → UI), at most 8 lines. 🔴 read fully: auth, permissions, workspace scoping, migrations, billing, public API shapes, user input. 🟡 skim. ⚪ mechanical, grouped by directory. -->

1. 🔴 `path` - why
2. 🟡 `path` - why
3. ⚪ `dir/` - N files, mechanical

**Watch out for:** <!-- the one or two decisions most likely to draw pushback -->

### Blast Radius

<!-- Flows that run changed code and must be retested, not files. One line each: flow (surface) - what to check. Include flows outside the diff, found by a consumer sweep; flag-off is a flow too. "none, sweep found nothing" counts. At most 8. -->

-

### Proof

<!-- Confidence is scored from the testing below, not from how the code feels. High: every Blast Radius flow verified, no open risk · Medium: core verified, gaps listed under Not verified · Low: key flows unverified, test before merge. Keep exactly one level, then one reason. -->

> [!IMPORTANT]
> **Confidence: High | Medium | Low** - reason, tied to Ran and Not verified

<!-- Link runs, do not recite pass counts. Never claim manual testing that did not happen. -->

- **Ran:** <!-- `just check` · `just test` (when operations/jobs/api/db changed) · `just evals` (when an agent, prompt or skill changed) · `just quality-report` delta · optional: `/review` outcome (or `just review`) -->
- **Try it:** <!-- steps written for the reviewer -->
- **Not verified:**

### Rollout

<!-- One bullet per item that applies: Alembic migration (reversible? backfill?), `PRISMOID_*` env flag, durable job names (a DBOS workflow or step name is never renamed in place: in-flight work recovers by name), deploy order, breaking contract, rollback. Else "None". -->

- None

### Media

<!-- Before and after for UI changes, else "N/A" -->

### Links

- **Issue:** <!-- Fixes #123; "None" is fine -->
- **Decision record / design:** <!-- docs/adr/NNN-....md, if any -->
- **Stack / follow-ups:**
- **Author notes:** <!-- optional: who to ask, and whether an AI tool wrote part of the change -->
