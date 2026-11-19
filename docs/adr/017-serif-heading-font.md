# ADR-017 — the serif heading trial, reverted; Inter rendered right

*Status: reverted (same session) · August 2026 · what remains: the `--font-heading` seam +
the rendering polish*

## Context

The product spoke one face (Inter Variable) at every size. Vamsi's direction (2026-08-01):
headers should be serif, the landing hero larger — energise a returning user. The system was
already shaped for a heading face: the Mira preset ships a `--font-heading` token and the
registry's Card/Sheet/Dialog titles read it, so a heading tier flips on one token.

## What was tried, in order (nothing thrown away — the record)

1. **Newsreader** (`@fontsource-variable/newsreader`, opsz) — replaced within the hour: too
   delicate and newspapery at product weights; thin strokes, low presence.
2. **Source Serif 4** (`@fontsource-variable/source-serif-4`, opsz) — the sturdier
   editorial, closest open face to the Tiempos-style serifs of current AI products. Better,
   still rejected on sight: a serif heading tier fought the product's compact Inter chrome —
   two voices where the app wants one. ("it's not good for some reason" — the reason is
   coherence.)

Both packages are removed. The swap-cost claim held: each change was two lines in `app.css`,
which is the argument for keeping the seam.

## Decision (standing)

**One face: InterVariable, using the full build.** `app.css` imports the **opsz** variant
(+ its true italic): Inter v4's optical-size axis serves the display cut at the `text-5xl`
hero and the text cut below, automatically. The `--font-heading` token stays, parked on
`var(--font-sans)`, and every heading site keeps `font-heading` — a future face experiment
remains a one-line change instead of a sweep.

**Rendering polish** (the "nice feel" half of the direction), on `html`:

- `-webkit-font-smoothing: antialiased` + `-moz-osx-font-smoothing: grayscale` — the
  Linear/Vercel feel; subpixel rendering reads heavy for a compact UI face.
- `font-optical-sizing: auto` — lets the opsz axis do its job.
- `text-rendering: optimizeLegibility` — kerning + ligatures always on.
- `font-synthesis: none` — never a faux bold or slant; safe because the true italic is
  imported and every weight is a real axis position.
- `-webkit-tap-highlight-color: transparent`; `::selection` styled as a quiet primary wash.

The contract lives in DESIGN.md § Type roles; /ds § Type renders the living specimens.

## Consequences

- Zero added dependencies (the two serif packages came and went uncommitted; Inter's opsz
  files replace its default build — same package, different import).
- The hero stays `text-5xl` (now `font-semibold` — Inter needs the weight a display serif
  carried intrinsically); the composer-placeholder copy from the same session stands.
- Anyone tempted by a serif heading tier later: read the trial record above first, then
  flip `--font-heading` locally before proposing it.
