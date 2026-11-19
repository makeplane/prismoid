# ADR-032 — The deliverable path law: dial > language > planner, stamped and shown

*Status: accepted · 2026-08-17 · lands with the ADR-033 workstream*

## Context

The Deliverable dial (ADR-015 M6a) was binding when set — planner rule 7, the typed fallback
floor, the planner-skipping fast path (ADR-019) — but on Auto the path was invisible and
undecided-looking forever: `tasks.deliverable` stayed NULL, nine call sites silently defaulted
to `document` (planning ×4, gates ×2, qa, longform, replan), and the user never saw which kind
the run had committed to. Language changed nothing deterministically: "make me a 25-slide
deck" with the dial on Auto did not even get its 25 detected, because `sizing.detect_units`
is kind-consuming and the kind defaulted to document. ADR-021 had recorded the boundary
honestly ("we do not goal-sniff") — but that promise was about the DIAL's determinism, not a
ban on resolving Auto.

User decisions (2026-08-17): named kind words are **binding** (like the dial); ambiguous goals
stay the planner's choice, stamped once planned; the resolved path is always **shown**; path
changes happen at **boundaries only** (composer pre-submit + follow-up turns) — no mid-run
replan.

## Decision

**One resolution, once per planning pass: dial > the goal's own language > the planner —
always stamped, always shown.**

- **The kind law** (`ai/policy/kinds.py`, sibling of the size law): curated word-boundary
  noun tables per kind, with the no-push temperament — only unambiguous artifact nouns
  resolve ("brief" is a size word; bare "page"/"sheet"/"board" never match; "script"/
  "program" are prose; "code review" is a document); a consumed noun ("summarize this deck")
  is an input reference, mirroring `sizing.is_input_ref`; two kinds cancel to None;
  unit-asks in a kind's OWN unit resolve when no noun does ("25 slides" → deck — which also
  fixes the sizing blindness, since the kind now exists before `detect_units` runs). Only
  the five dialable kinds are ever emitted. Every exclusion is a counter-example test
  (`test_kind_policy.py`).
- **The source column**: `tasks.deliverable_source` `String(8)` — `"user"` (the dial) |
  `"auto"` (detected) | `"plan"` (stamped from the validated plan's final publishing step,
  `_final_kind`). Backfill: existing rows with a deliverable read `"user"` — the dial was
  their only writer. Fleet never resolves, never stamps.
- **Binding, but appealable**: an `auto` kind constrains exactly like the dial (rule 7 +
  `KIND_AGENTS` floor) but only `"user"` takes the planner-skipping fast path
  (`planning.py`, the `resolved is not None and source == "user"` branch) — a wrong guess
  must never become an unappealable one-step plan. `_auto_pins`' KIND_SKILLS shortcut is
  likewise dial-only: auto/plan kinds ride the router as undialed asks always did (document
  is deliberately absent from KIND_SKILLS — an auto-stamped memo must not lose
  report/summarize/email routing).
- **Per-turn re-resolution** (`kinds.resolve_turn`, the ADR-029 M2 shape): turn dial > fresh
  language naming a DIFFERENT kind (re-stamps `"auto"`) > the stamp; confirming language
  never downgrades a user dial; a silent turn continues the thread's path. `TaskContinued`
  gained `deliverable` beside `scale` — the row alone cannot distinguish this turn's choice
  from the resolver's own past stamp.
- **Shown everywhere**: `TaskSummaryOut.deliverable_source` on the wire; the web chip beside
  `size_marker` composes its label client-side (`kit/artifact-icon.ts` stays the ONE
  vocabulary home — a kind has no unit math for the server to own, unlike `size_marker`);
  tooltip names the source ("set by your dial" / "detected from your ask" / "the planner's
  choice"). The `askedForStamp` revalidate latch generalized from once-per-mount to
  once-per-TURN — which also fixed the pre-existing staleness of the size chip after a
  resizing follow-up. `DeliverableDial` promoted out of the home route into
  `composer/dials.tsx` and mounted in the follow-up composer (CLI parity:
  `tasks follow-up --deliverable`, with the `board→collection` alias).
- Forks inherit `(deliverable, deliverable_source)` verbatim — a plan-stamped parent kind
  never becomes a fake user dial on the branch.

## Consequences

- An auto-detected kind + an explicit unit-ask in that kind's unit ("60-slide deck") may now
  enter the deterministic sectioned lane — accepted deliberately: that is binding explicit
  language, and it is the fix for the unit-detection blindness.
- The remaining `else DOCUMENT` fallbacks (gates, qa, longform, replan) stay as legacy-row
  belts; the stamp makes the row authoritative for new work. Replan reads the stamp — the
  no-mid-run-replan decision, in code.
- Migration `b8c0d2e4f6a8` (column + backfill). Contract regenerated (TaskContinued field +
  TaskOut field; still 32 variants — no new event type). Zero new dependencies.
- Detection is regex-deterministic and therefore literal: negation ("don't make a deck") and
  sarcasm are beyond it. The chip makes any miss visible in seconds, and the next turn's
  dial or language corrects it — the appealability the fast-path gate preserves.

## Addendum — the document format dial (2026-08-18)

User ask, next morning: *"what do I select if I need markdown/html?"* The document's FILE
FORMAT became a first-class dial with the kind's own laws — this is the section every
"ADR-032 addendum" comment in the code points at.

- **The column**: `tasks.deliverable_format` (`d0e2f4a6b8c0`) — `"md" | "docx" | "pdf" |
  "html"`, documents only, NULL = the words decide (markdown is delivery's default). No
  backfill: every existing row's format was words-decided, which NULL means.
- **Format implies document**: on create and follow-up, a format alone sets
  `deliverable="document"` — picking "PDF" IS picking a document, and the implication is
  the user's own dial (`source="user"`). Paired with any other kind it is a 422 naming the
  conflict; fleet refuses it like the deliverable itself (legacy `council` creates are
  normalized to fleet before the guard, and a kind-CHANGING turn dial clears the stale
  format at the door — the row must never serve a (deck, pdf) pair).
- **Per-turn law** (`kinds.resolve_turn_format`): turn dial > fresh format words > the
  stamp; a kind change clears it. Format words count only in ASK position ("a PDF", "as
  markdown", "convert it to markdown") — topic position ("a one-pager on pdf
  accessibility", "a report on landing page performance") stays silent, or a subject would
  deterministically route the typesetter against a prose ask. `TaskContinued.
  deliverable_format` carries the turn's dial beside `scale` and `deliverable`.
- **The md-only trap (ADR-021's recorded boundary), closed**: a dialed document fills its
  format from the goal's own words, and the fast path routes docx/pdf/html to the
  TYPESETTER with the format on the acceptance and the format's mechanics skill pinned
  deterministically (`FORMAT_SKILLS`: docx → docx, pdf → pdf, html → page). md pins
  nothing — the dialed-document posture (prose routing stays the router's, and the router
  runs only on auto/plan kinds, as ever).
- **Every lane honors the stamp** (hardened by the 2026-08-17 review): the sectioned lane
  carries `doc_format` into the architect asks AND overrides the outline's `format_target`
  at expansion (an ask-sentence is not a guarantee); the PLANNER path names the format in
  the brief and the planner-failure fallback floor becomes the typesetter for docx/pdf/html
  — an auto-stamped "give me a PDF" can no longer ship markdown under a chip that says PDF.
  html never enters the sectioned lane (no `md_to_html` walker); it falls through to
  one-shot/planner.
- **Shown, one control**: the chip grows the format ("Document · PDF"); the composer keeps
  ONE dial (user decision, same day — a second dropdown beside the first read as clutter).
  *Amended 2026-08-18 v2 (user feedback: the flat "Document / Document · Markdown / …" run
  "is confusing" — it read as five sibling kinds):* the document family NESTS. Six top
  rows (Auto · Document ▸ · Collection · Spreadsheet · Presentation · Code); clicking
  **Document** selects it and closes — words pick the format — while the row's chevron
  (hover or click) flies out Words decide · Markdown · Word · PDF · HTML page, so touch
  and keyboard users keep both doors. The dial is the kit `DropdownMenu` now (the vendored
  Select has no submenus; Menu's own dismissal proved sound where Select's needed the
  scoped closer), a muted family ✓ marks the Document parent while a format is the real
  selection, and `splitDeliverableChoice` still maps every row back to the wire's two
  fields. CLI: `--format` on `do` and `tasks follow-up`.

## Post-review amendments (2026-08-17, three-lens + machine review, fixed same day)

The kind law's detection semantics were hardened; each item is a counter-example pin in
`test_kind_policy.py`:

- A consumed noun now retires its COUNTS too — "digest my 30-slide deck" resolves nothing
  (the unit fallback had re-resolved the very noun the discount just retired, and sizing's
  narrower input fence let "my"-chained sources through).
- Format nouns resolve only in ask position (`_FORMAT_ASK`) — the topic trap above.
- Compound codes are prose subjects: dress/QR/promo/zip/…/penal code never stamp CODE.
- Transformation verbs (turn/convert/transform/rewrite/reshape/repackage) mark their source
  nouns as inputs for the NOUN law, mirroring sizing's summarize family.
- `KindHit.asked` (ask-position evidence: article chain, literal count, or a bare
  into/to/as conversion hinge) gates `resolve_turn`'s override of an EXISTING stamp — a
  bare mention ("…the deck we showed them") is conversation, never a re-route; "turn my
  notes into slides" still is. Turn 1 keeps the plain latitude.
- The stamped format reaches the planner brief, the planner-failure floor, and the
  sectioned lane's expansion (folded into the addendum above).

**Amended 2026-11-15 (ADR-092 §8, the v1 doors deleted):** The `auto` kind stamp from the request's words is no longer written (the detectors are deleted); stored `auto` rows stay readable and, as before, never bind a later turn.

**Amended 2026-11-15 (ADR-092, the planner and router deleted):** The stamped format reaches the Lead's facts and its floor (`lead_check.floor`, `planning_turns.floor_for`); a USER-dialed kind still pins its format skill on the task (`planning_turns.pin_dialed_skill`), and prose routing is the Lead's per-step skill choice, never a router's.
