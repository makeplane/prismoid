# Deck design - the full method

The body's short law is enough for most decks. Read this when the deck is the deliverable that
matters - a pitch, a board pack, a launch narrative.

## Choose the palette from the subject

Name the subject, then let it pick the colors. A forestry co-op and a fintech API should not
wear the same deck. One color dominates (roughly two-thirds of the visual weight), one or two
neutrals support, one accent punctuates. Some working sets (hex, no `#` needed in python-pptx's
`RGBColor.from_string`):

| Direction | Dominant | Neutral | Accent |
|---|---|---|---|
| Institutional calm | `1F3A5F` deep navy | `EEF1F5` fog | `C9A227` brass |
| Field & growth | `2E5339` fir | `F4F1E8` unbleached | `D96C2C` clay |
| Clinical clarity | `15616D` petrol | `FAFBFC` near-white | `FF7D00` signal |
| Industrial heat | `3B3B3B` graphite | `ECECEC` ash | `C63D2F` furnace |
| Quiet luxury | `2B2118` espresso | `EFE9E1` linen | `7A9E7E` sage |

Test: swap your palette onto an unrelated deck - if it still "works", it was generic; choose
again with the subject in mind.

## Layout patterns (vary them - repetition is the tell)

- **Split**: text left, one visual right (or mirrored). The workhorse.
- **Big stat**: one number at 60–72pt, a small label beneath, one supporting line. Nothing else.
- **Rows**: 3–4 icon-or-number + header + one-line-description rows for processes and pillars.
- **Grid**: 2×2 for comparisons and portfolios; every cell the same inner structure.
- **Full-bleed statement**: dominant-color background, one sentence in the neutral, for the
  open, the turn, and the close.
- Sequence them deliberately: statement → split → rows → big stat → grid → statement reads as
  rhythm; five splits in a row reads as a template.

## Type

- One family throughout is safe; two (a heavier display for titles, a plain body) is the most
  contrast a deck needs. Name widely-installed faces - Arial, Calibri, Georgia, Times New
  Roman - the file renders with the viewer's fonts, not yours.
- Scale: title 36–44 bold · section header 20–24 · body 14–16 · caption 10–12. Keep the scale;
  a deck with nine sizes has none.
- Body text left-aligned, ragged right. Center only titles and full-bleed statements.

## The avoid-list (each one reads as machine-made)

- Accent bars, underline strips, or edge stripes as decoration - set hierarchy with size,
  weight, and space instead.
- The same layout on every slide.
- Text-only slides, or bullets deeper than two levels.
- Centered body paragraphs; justified text.
- A chart with default everything - title it with its takeaway, label the values, use the
  palette, drop the legend when there is one series.
- Filling every inch. The margin is doing work; leave it alone.
- Low contrast: light text on light fills, dark on dark. When in doubt, measure - body text
  wants the strongest pairing on the slide.

## Self-check before publishing

Run the bundled `check_deck.py`, then read its per-slide text dump top to bottom and ask: do
the titles alone tell the story? Does every number on a slide appear in a source? Is any slide
making two points? Fix in the build script and rebuild - never hand-patch a saved file.
