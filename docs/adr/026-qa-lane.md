# ADR-026 — The QA lane: a pre-delivery editorial gate (ADR-019, amended for long form)

**Status:** accepted · 2026-08-08
**Context:** ADR-019 made answer-first the law — publish → done → deliver; the receipt signs
after, never blocking; the Retry button is the corrective loop — and preserved exactly one
lane-scoped exception, the fleet's in-line verify (ADR-010). The long-form lane breaks
ADR-019's economic premise: Retry-the-turn assumed ~30–60 s regenerations; at two hours and
real dollars the Retry button is a punishment. And with the lane's receipts carefully removed
(ADR-025), the lane needs its own quality layer — the user's word for it: QA.

## Decision — the second lane-scoped exception, the fleet's shape exactly

Steps flagged `StepSpec.qa` (set only by the long-form expansion, on assembly) enter
VERIFYING after harvest and run one QA pass:

1. **Deterministic pre-checks first** (`ai/verification/precheck.py`, run IN a fresh no-egress
   frame — python-docx/pypdf live only in the frame image): extraction · every outline
   section present and in order · length vs target ±30% · figure captions/markers present ·
   heading sanity · **mid-thought truncation** (the classic shipped defect: a quotation cut
   mid-sentence inside a green docx) · link shape. All `qa:`-prefixed; never enter a
   VerificationReport; the same run writes `/work/extracted.md` — one extraction, two
   consumers.
2. **Then the editorial read**: the `qa` agent — LONG_CONTEXT (the whole value is reading the
   TAIL; the judge's 40k-char clip is the standing admission that lane cannot), NO tools (a
   reader that chooses what to open can choose not to look — operations assembles the full
   text into the prompt, clipping only past a 600k-char belt and ANNOUNCING it),
   same-family-tolerant (editorial ≠ adversarial; cross-family stays the receipt's posture),
   `plannable=False` (a plan that picks its own gate picks a friendly one), never asks (a
   stalemate ships with notes; the user's follow-up turn is the unbounded loop, ADR-018).
3. **The outcome law** (pure, `ai/verification/qa.py`): a check FAIL is a revise floor the agent
   cannot lift · the agent's REVISE stands on clean checks · both clean = PASS · read
   unreachable with clean checks = PASS_UNREAD, shipped with the note visible and logged
   loudly (on a receipt-less lane an unread gate is a product regression, not a footnote).
4. **Revise** feeds the findings — location + one imperative instruction each — into the
   assembly's retry gotcha (`_verify`'s wording law) for ONE corrective rebuild
   (`MAX_QA_ATTEMPTS=2`, the fleet's budget); past it, `_accept_with_qa_notes` ships honestly
   with the flags in the thread — deliberately never the word "unverified", which belongs to
   the receipt lane. Every pass lands one durable `qa.reviewed` frame (the 32nd variant):
   verdict, findings, reviewer from the LEDGER — the follow-up turn's reference.
5. **Answer-first restated**: nothing sits between the artifact and the human except the
   lane's own quality bar. Standard and fleet are byte-for-byte untouched. QA adds minutes to
   hours, bounded, degrades to notes, and any QA infrastructure failure costs the gate, never
   the thread.

## Economics
One LONG_CONTEXT read (~150k tokens in) + free pre-checks + one ~300 ms frame ≈ <5% of a
long-form task's cost and ≤300 s wall clock, against hours and full cost for a blind retry.

## Deferred, named
The visual pass (rasterized rendition pages via poppler-utils — a §3 row — read as
BinaryContent; "does page 40 look right" is a check nothing else can make) · `qa.reviewed` in
the thread-transcript renderer (turn N+1's planner reading the notes without a query) · the
web's tree/epilogue rendering of the verdict (the façade accepts the frame today) · a
per-task QA opt-in dial for standard tasks.

## Amendment 2026-11-14 - the judge reads

The editorial read is the `judge` agent's (ADR-019's 2026-11-14 amendment): one seat, chosen as the
receipt chooses it, in place of the `qa` agent and its panel of up to three families. Its FAIL with
findings is the revise; the outcome law is unchanged.
