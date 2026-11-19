# ADR-019 · The answer is primary; verification is its receipt

**Status:** accepted · 2026-08-07
**Replaces:** verify-before-done for standard tasks (§8.5's `harvest → verify → done` weave,
M2.4's ladder doctrine "everything is verified, or it is not done") — removal, replacement and
reason recorded here per the standing rule. ADR-010's fleet weave is explicitly retained.

## Context

Verification sat between the agent publishing and the user seeing anything: a fresh verify
frame (~200 ms create + suite exec), then a cross-family judge (90 s ceiling per candidate),
per artifact, on the step queue, before the step could go `done` — and only then delivery.
Two consequences, both reported by users as "collections fail and everything is slow":

1. **Latency**: the answer waited minutes on machinery that exists to *describe* it (M7.4:
   "stalled in `verifying` for minutes and did not deliver").
2. **Fragility**: any infrastructure failure inside `verify_artifact` — the verify frame
   refusing to create, a blob fetch, a check-parse edge — raised through the ladder and
   **failed the whole task**. Verification infrastructure fragility became answer fragility.
3. Collections compounded it: the publish hand-off interpolated model-authored strings into a
   bash script (a quote in a title refused the publish, four times, then "nothing was
   published" escalated the task), and near-miss JSON that sailed through the content-blind
   door was refused later by the strict suite.

## Decision

1. **Standard lane: published means done.** `execute_step` completes the step the moment the
   artifact exists (`running → done` is now a legal transition); narration, promotion and
   delivery follow immediately. Nothing sits between the artifact and the human.
2. **The receipt follows on its own job.** Harvested artifacts enqueue `verify_artifact`
   (deterministic id `verify-{artifact_id}`, own `verification` queue, concurrency 4) after
   the step's commit. The job runs the SAME pipeline — suite, cross-family judge, Ed25519
   signature, `Verification` row, `verification.signed` frame — post-delivery, on the same
   stream (the web keeps its socket open on settled tasks, so the verdict lands live in the
   thread; `VerdictBadge` renders the interim as "Pending"). A receipt that cannot be signed
   costs the receipt, never the thread: every job failure is swallowed to a log and the
   artifact honestly stays `verdict: null`.
3. **The fleet keeps its in-line weave (ADR-010).** A seat's corrective retry rung and the
   ship-unverified-after-budget guard are part of the mode's contract the user opted into;
   `verifying` survives as the fleet's lane in the step machine.
4. **The produce ladder narrows to produce failures.** "Nothing was published" still retries
   targeted (≤3) and escalates with a diagnosis — there is no answer to show. Verify-fail
   auto-retries are gone from the standard lane; the **user's Retry button** is the corrective
   loop now (`POST /tasks/{id}/retry` = run the latest ask again as a new turn, whole thread —
   including what went wrong — in the planner's hands).
5. **Collections never fail the thread.**
   - The publish hand-off is injection-proof by construction (`ai/frames/publish.py`): a
     static runner + JSON args over the tar transport; no model-authored string ever touches
     a shell. A missing file exits with a sentence the model can act on.
   - Harvest runs the **collection normalizer** (`schemas/collection_repair.py`): a
     deterministic, conservative salvage — stray select values become options, timestamps
     become dates, a board that lost its group falls back to a groupable field or an honest
     table — ending in a strict `Collection.model_validate`, with every change recorded on
     the provenance (`ManifestEntry.repairs`). Unrepairable bytes ship as published; the
     reader's "can't be read" card is the floor. The lockstep law extends: the 21 mutations
     the model refuses are the 21 the normalizer must save (`test_collection_repair.py`).
   - Harvest hard-fails became rungs: hash mismatch, unreadable manifest, missing published
     file are `PolicyDenied` (a failed attempt) — never an uncaught task death.
6. **The end of a response is visible.** A settled turn renders the action row — Copy, 👍/👎
   (durable `task.feedback` frames, newest wins, reset per turn), Retry — under the answer.

## Consequences

- Delivery latency for standard tasks drops by the whole verification phase; verify frames no
  longer compete with step frames (own queue).
- An artifact may be seen, downloaded, even revised before its verdict exists — `verdict:
  null` ("nobody has looked yet") is a first-class, already-tolerated state on every surface;
  the receipt arrives in-thread as the same quiet `Verification: …` marker.
- The CLI's `--wait` follower exits at `task.delivered` and does not see the late receipt —
  by design: the user asked for the answer. `computer artifacts show/verify` read it any time.
- `preview_sha256` (the xlsx render sidecar) also arrives with the receipt; `has_preview`
  was already tolerant.
- The escalation diagnosis text and its advice (`schemas/failures.ADVICE`, keyed by the last death's
  `FailureKind`) describe produce failures in the standard lane; the fleet never escalates with it
  (it ships unverified), so the advice carries no verifier vocabulary.

## Rejected

- **Deliver-then-auto-retry on a failed verdict**: re-running the agent after the user has
  the answer either surprises them with a silently changed artifact or spams revisions.
  Judgment belongs on the receipt; correction belongs to the user's Retry.
- **Dropping verification entirely**: the receipt is the product's honesty ("re-derived,
  re-checked, signed"). It moved; it did not die.
- **Repair-at-publish (in-frame)**: the frame has no pydantic and the engine stays
  persistence-free; harvest is host-side with the bytes in hand — one door, one normalizer.

## Addendum (2026-08-07) — the text lane completes the design

A live failure showed the typed-output lane had covered collections and left documents on the
old ceremony: a theory-of-consciousness task rode its whole document inside ONE `write_file`
tool call's JSON args, hit Anthropic's **4,096-token default output cap** (no `max_tokens` was
ever set), and burned **thirteen identical truncations across three ladder rungs — 9m41s,
$0.31, task failed**. pydantic-ai's own error named it verbatim each time; the ladder retried
the same doomed call because nothing about the conditions changed between rungs.

Two laws close the class:

1. **The output ceiling is ours, never a provider default.** `default_model_settings()` sets
   `max_tokens=16_000` — above any sane deliverable, below every catalog model's output limit,
   under the no-streaming-required threshold for the judge/planner paths.
2. **A text deliverable is the run's output, system-published** — the writer joins the
   organizer: `AgentDef.publishes_output=True` + `output_ref=None` ⇒ the model's prose IS the
   document; `execution._publish_document_text` names it after its own `# Title` (falling back
   to the plan's declared title), writes the bytes, and walks them through the one door with
   the frame's inputs as sources. The writer's tools shrank to `read_file`; its TextParts
   stream on the DRAFT channel (`/work/draft.md`), so the document composes in the artifact
   pane and the message lane stays silent. Narration became a deterministic receipt line.

Proof, same ask, same model: **59s, 7 calls, $0.07, attempt 1, delivered** — receipt signed
0.3s after delivery on the verification queue. The web stopped mumbling about the machinery
while it happens: `escalation.raised` renders the FULL diagnosis card (the old marker clipped
`why` at its first line — the page showed "The last, because:" and nothing), `retrying` reads
as "Attempt N of 3 didn't produce the deliverable — taking another pass: <finding>", and the
topbar spend folds `usage.recorded.cost_usd` live (it was frozen at the loader snapshot —
"$0.0000 · 17 calls").

### The verdict law (same day, after the data spoke)

481 document verdicts on the ledger showed the receipt was misleading in bulk: **160 artifacts
from unsourced tasks — creative and original work — had zero passes, ever**, because every
grounding check skips without sources and the old aggregation law froze the verdict at
UNVERIFIED ("a judge's approval must never launder it") even when the cross-family judge read
the work against its acceptance criteria and passed it. Verification read as "fails most of
the time" when the work was fine — a citations rubric applied to fiction.

The law is now **the rubric follows the work**: a deterministic FAIL remains a floor nothing
can lift, a judge FAIL still sinks anything — but when the suite had nothing substantive to
examine, the judge's criteria read IS the substantive examination and its PASS stands, but only
when the judge was shown the content (`judge_read_content`): a binary reaches it as the text its
suite extracted in the frame, and a placeholder view never promotes. Only work nobody examined
keeps the UNVERIFIED mark, which is what makes the mark mean something.
Judge breadth rode along as data: `gemini-flash-latest` gained the `judge` class (flash-lite
had been the ONLY non-Anthropic judge — the cheapest model in the catalog grading everything
Anthropic wrote). Proof: an unsourced fable, delivered in 27s, receipt **PASS** signed by the
google family against the acceptance criteria.

## Amendment 2026-10-15 - an action Step's done is proof-checked (agent loop, round 1)

A computer step ends on a typed Outcome, and its `done` is checked before it counts: a URL plus
a page phrase present in the current page, a path in `/work`, or a published artifact. Blank
fields are absent, a URL alone is not proof, and a phrase under 3 characters is not proof. A
failed proof gets ONE more turn, then reads partial. The closing turns spend the step's
REMAINING wall clock (ADR-027 bounds the whole run), so a slow main run may get no proof turn
and reads partial. Documents are unchanged; the receipt still signs after delivery.

## Amendment 2026-11-14 - the proof nudge is a request of the same run

The proof check, the hand-off nudge and drive-first run as output checks inside the run
(`ai/capabilities/output_checks.py`): a failing check is a `ModelRetry` from the output hook, one
more request on the same guard, limits and call log, never a new run or segment. At most two per
run (drive-first, then one of hand-off or proof); the output-retry budget is the schema's 3 plus
those 2. A nudge is sent only when the run is under its request ceiling with one answering turn of
its wall left; otherwise the ending stands. The model's Outcome is never rewritten: a `done` still
unproven settles `partial` beside it, as before.

## Amendment 2026-11-14 - the judge falls back when only one key exists

Replaces "never a same-family judge": most deployments hold one provider key, and with it no judge
ever ran. `gateway/resolve.judge_seats` now seats the judge in the judge chain's order, furthest
from the maker first: a model of another family with a key, else another model of the maker's
family, else the maker's own model in a fresh context that sees only the rubric and the extracted
document. The receipt names the seat (`JudgeFamily`: other family / same family / self-checked);
only when no judge row can be built is the work judged by nobody. The deterministic suite always
runs, and a deterministic FAIL stays a floor.

## Amendment 2026-11-14 - one judge, typed checks, pages to a judge that can see

The judge also does the long-form lane's editorial review (ADR-026): the `qa` agent is gone (its name
resolves to `judge`), the review runs on the same seats and the same candidate loop as the receipt, and
one in-line review drives both lanes that review before delivery. A check carries its `kind`
(structural or substantive) and only a substantive pass carries a verdict; a check that does not
declare it is read from its namespace. The report carries `schema_version` (2); a report stored without
it is version 1 and still verifies over the bytes it was signed in. A `.docx`, `.pdf` or `.pptx` is
rendered to pages in the verify frame when a judge seat can see, and those seats are shown them.

## Amendment 2026-11-15 - the goal and the act join the proof

A `done` on a step with `acceptance` accounts for every line in `Outcome.criteria` (`line`, `met`,
`shows`); a line not accounted for, or said unmet, fails the check. A `done` whose run last changed
state with a guarded browser act (click, type, fill, select, press) that the browser refused or that
changed nothing fails it too: each act's `ActCheck` is read from the sidecar's own facts, never from
the page. Both are read from typed values, so page text can flip neither, and they cost no model
call: their reasons join the proof's in the one closing nudge, then the `done` settles `partial`.
A model validator reading the page stays an option if the web suite shows misses.
