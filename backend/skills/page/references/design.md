# Page design - the full method

The body's laws make the page correct; this file makes it *this* page and nobody else's.

## Ground every choice in the subject

Before any CSS: name the subject, its audience, and the page's one job, in one sentence each.
Every distinctive choice - palette, type, the signature element - comes from that world. A
harbor-tide tracker wants navigation-chart restraint; a hot-sauce launch wants heat. If your
palette would survive being swapped onto the other one, it was a default, not a decision.

## Palette (4–6 named values, committed in `:root`)

```css
:root {
  --ink: #1C2321;        /* body text - earns ≥4.5:1 on --paper */
  --paper: #F7F5F0;      /* the field everything sits on */
  --dominant: #2E5339;   /* the subject's color; ~2/3 of the visual weight */
  --accent: #D96C2C;     /* punctuation: links, the one button, the signature */
  --muted: #5C6662;      /* captions, rules, secondary text */
}
```

One dominant, one accent, quiet neutrals. Dark pages flip ink/paper and *raise* the accent's
lightness (contrast still ≥4.5:1). Never spend the accent everywhere - punctuation, not prose.

## Type from system stacks (no font files, no services)

Pair a display voice with a working body voice:

- Editorial: `Georgia, 'Times New Roman', serif` display · `system-ui, sans-serif` body
- Technical: `system-ui, -apple-system, 'Segoe UI', sans-serif` display ·
  `ui-monospace, 'SF Mono', Menlo, monospace` for data
- Set a real scale and keep it: h1 clamp(2.2rem, 5vw, 3.5rem) · h2 1.5rem · body 1rem/1.6 ·
  captions 0.85rem. Line length 60–75 characters (`max-width: 65ch` on prose).

## Layout patterns

- **The column**: `main { max-width: 72rem; margin-inline: auto; padding-inline: 1.5rem }` -
  everything breathes inside it.
- **Hero as thesis**: open with the most characteristic thing in the subject's world - a
  statement, a number that matters, a drawn SVG - not a stock big-number card.
- **Rows and grids**: `display: grid; grid-template-columns: repeat(auto-fit, minmax(16rem,
  1fr)); gap: 1.5rem` wraps honestly on phones without a single media query.
- **The signature element**: ONE memorable device - a chart drawn in inline SVG, a border
  motif from the subject, an interactive detail - and keep everything around it disciplined.

## Motion and interaction

- Vanilla JS, inline, small: a nav toggle, a tab switch, a computed figure. State lives in the
  DOM; no framework, no build step.
- Animate only what serves the subject (one entrance, hover confirmations) and wrap it:
  `@media (prefers-reduced-motion: reduce) { * { animation: none; transition: none } }`.

## The floor (non-negotiable, invisible when right)

- Contrast: body ≥ 4.5:1, large text ≥ 3:1 - check the actual pairs you shipped.
- `:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px }` on every
  interactive element.
- Alt text that says what the image *means*; `aria-label` on icon-only buttons.
- Keyboard order follows reading order; nothing traps focus.

## The avoid-list (the templated tells)

- Purple-gradient hero + centered everything + three identical feature cards.
- Decorative accent bars and edge stripes as section markers.
- A dark theme that is just `#000` behind unadjusted colors.
- Rounded-everything at one radius; pick a radius language (sharp, 4px, or pill) and mean it.
- Lorem ipsum or invented statistics - the page's figures obey the same grounding law as a
  memo's.
