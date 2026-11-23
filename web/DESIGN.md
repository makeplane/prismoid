# DESIGN — how a screen gets built here

The base is **Plane's design system**: Propel's tokens, with Prismoid's black accent, through
`@prismoid/blocks` (ADR-074), and Propel's components, imported directly as Plane's apps do.
Our semantic layer sits on top. This file is the contract that keeps new pieces from becoming
patchwork: if you're about to type a class recipe that names a _pattern_ — a pill, a list, a
card, an empty state — it already exists in `~/components/kit`, or it should be added there
first. Rule of thumb: **typed twice → it's a kit primitive.**

## Layers (who may do what)

| Layer                                       | Contents                                                                                                                                                                                                                                                                                                                                                                                        | Rules                                                                                                                                                                                                                                                                                                                          |
| ------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `@prismoid/blocks` (`packages/blocks/src/`) | Prismoid's composites rebuilt on Propel (ADR-074): `sidebar`, the composer's `prompt-input` · `attachment` · `spinner`, the thread's `bubble` · `marker` · `message-scroller`, `code-block`, `card`, `toast`, and `cn`                                                                                                                                                                          | Imported by subpath (`@prismoid/blocks/sidebar`), no barrel. Built from Propel parts and Propel's token classes only; Propel components get no `className`/`style` and their variants are always passed. Presentational: data in through props.                                                                                |
| `@makeplane/propel`                         | Plane's primitives: buttons, badges, fields, choice controls, tabs, tables, menus, popovers, dialogs, drawers, toasts' substrate (ADR-074; the shadcn kit it replaced was deleted in stage 4)                                                                                                                                                                                                   | Imported by subpath (`@makeplane/propel/components/button`). No `className`/`style` on a Propel component (layout goes on a wrapper or Base UI's `render`); content is a prop (`label`); every variant axis is passed. Where a part cannot hold a behaviour, Base UI's primitive on Propel's tokens (ADR-074 lists the three). |
| `components/kit/`                           | The house vocabulary — `PageHeader, Section, EmptyState, LoadingState, ComingSoon, Pill, Rows, ToggleChip, IconTile, MetaChip, LabeledDivider, FormError, Money, CopyButton, StatusBadge, StatusDot, StatusMark, statusLabel, VerdictBadge, Markdown, StepTimeline, QuietDisclosure, DisclosureChevron, artifactIcon, artifactKindLabel, SearchBox, LoadMore, UserBubble, initials` + `tone.ts` | The ONLY place domain visuals are defined. Screens import from `~/components/kit`.                                                                                                                                                                                                                                             |
| `components/artifact/renderers/`            | Feature vocabulary for artifact kinds — one registry (`registry.ts`), one renderer file per kind, the `DownloadCard` floor                                                                                                                                                                                                                                                                      | Composes Propel + blocks + kit; **no new visual primitives**. Dispatch keys on the server's `artifact.render` hint, never a client-side type list (ADR-015). Adding a kind = one renderer file + one registry line; the shell owns fetch/boundary/fallback.                                                                    |
| `components/icon-picker/`                   | The project's face: `lucide:`/`emoji:` wire grammar (`value.ts`), curated catalogs (`catalog.ts`/`emoji.ts` — data, not logic), the `ProjectIcon` atom, the content-only `IconPicker`                                                                                                                                                                                                           | Composes Propel + blocks + kit; hues come ONLY from the categorical ramp (`HUE_TINT`/`HUE_BG` — user data, never status tones). Adding an icon = one catalog row; the server stores the string, never interprets it.                                                                                                           |
| `components/layout/`                        | Shell chrome: sidebar, breadcrumbs, auth frame, route error, dev footer                                                                                                                                                                                                                                                                                                                         | Composes Propel + blocks + kit; no new visual vocabulary.                                                                                                                                                                                                                                                                      |
| `app/routes/**`                             | Composition only                                                                                                                                                                                                                                                                                                                                                                                | No hand-rolled pattern instances; route-local components stay in their route dir and are never imported across routes.                                                                                                                                                                                                         |

## Type roles (sizes are roles, not choices)

The root is **16px**, Plane's (ADR-074; it was 17px). Every rem size below rides on it, so
`text-body-sm` body is 14px rendered, Plane's body size. Body text is Plane's regular weight (450).

**One face, rendered right (ADR-017): InterVariable's opsz build, everywhere.** The serif
trial (Newsreader, then Source Serif 4) is recorded and reverted in ADR-017; what remains is
the seam — `--font-heading` parked on sans, heading sites keeping `font-heading` — so a face
change stays a one-line experiment. The rendering polish lives on `html`: antialiased
smoothing on both engines, `font-optical-sizing: auto` (the opsz axis serves Inter's display
cut at display sizes), `text-rendering: optimizeLegibility`, `font-synthesis: none` (true
weights and the imported true italic — never faux), tap-highlight cleared; `::selection` is
a quiet primary wash.

- **Propel's scale** (ADR-074): every size is a semantic Propel class that sets size, line
  height and weight together: `text-body-sm-regular` (14px body), `text-caption-md-regular`
  (12px meta), `text-h4-regular` (20px titles), and the rest of Propel's `body-*`, `caption-*`
  and `h1..h6` steps. A weight class beside it (`font-medium`, `font-semibold`) wins. Tailwind's
  `text-xs/sm/...` are gone from the app; `cn` (`@prismoid/blocks/cn`) knows Propel's names.
- **Hero** (landing ONLY): `font-heading text-display font-semibold tracking-tight` - Prismoid's one
  display size (48px), on the one page that IS the composer.
- **Title** (page or detail): `font-heading text-h4-regular font-semibold tracking-tight` -
  always via `PageHeader` (`as="h2"` inside a drawer). One title size everywhere.
- **Section label**: `Section`'s `text-caption-md-regular font-medium tracking-wide uppercase muted`.
- **Body**: `text-body-sm-regular`. **Meta/labels**: `text-caption-md-regular`. Rendered markdown's
  h1 is the lone `text-h5-regular`. Its tables read at body size in one ringed, rounded frame
  (`.table-frame`, which also scrolls a wide one) with row rules only and a caption-size muted
  header; inline code pads in `em` so it never leaves a gap before punctuation (2026-11-12).
- **Mono is a role, not a fallback** (ADR-053): `--font-mono` = JetBrains Mono Variable,
  self-hosted beside Inter; every `font-mono` (code cards, wire ids, kbd) rides the one
  token. ADR-017's one-face law is about the HEADING seam and is untouched by this.
- Badges are Propel's at `size="xs"` (a 12px caption); there is no smaller text step.
- Weights: `semibold` for titles + hero, `medium` for emphasis/labels, `regular` (Plane's 450)
  otherwise. No `bold`.

## Project rhythm

- Page = shell container (`w-full px-gutter py-8`) + **`space-y-6`** wrapper. Always 6.
- **Section tabs never restate their name** (2026-08-01): the breadcrumb IS the title, so
  /tasks, /projects — and every future top-level tab — render NO in-page `PageHeader` and no
  explanation line; the page's search (`w-56`) and primary action ("+ New …") project into
  the topbar via `TopbarPortal`, in that order — tree order is slot order. `PageHeader`
  remains the title recipe for detail surfaces and drawers; an EMBEDDED browser (a project's
  Tasks tab) keeps its search inline — the topbar belongs to the page, not a tab.
- **Section-tab anatomy is ONE scaffold** (`layout/section-tab.tsx`): a `min-h-8` control
  row + content at `space-y-4`; a list and its `LoadMore` group at `space-y-3`; list rows
  are `px-4 py-3`. The row must EARN its band: real controls or a quiet result count —
  and a tab whose standing controls all ride the topbar (/tasks: search · Filter ·
  Display · + New) renders the in-page row ONLY when it has something to say (the
  selection bar, or active-filter chips). Two lone icons over a dead band was the
  counter-example (2026-08-01). TaskBrowser implements the anatomy internally; simple
  tabs compose `SectionTab`.
  **`px-gutter` is THE app inset** (`--spacing-gutter` in app.css — ONE knob, and
  RESPONSIVE: 1.25rem under `md`, 2rem from `md` up; the token lives outside the inline
  theme block so the utility references the var and the media override can retune it):
  the shell header, the page container, and the task page's chrome/column all speak it —
  one left edge from crumb to composer. Below `md` the shell header opens with the nav button
  (the sidebar is an off-canvas sheet there, its own collapse control inside it), and the trail
  keeps only the page's crumb, which truncates; from `md` up section crumbs never shrink, only
  the page's does (2026-11-12). Full-bleed bars inside a page use
  `-mx-gutter px-gutter` (they mirror the container's padding). **Pane surfaces** (the
  inbox's two-pane) speak `px-gutter-pane` — `max(0.75rem, gutter/2)`, DERIVED from the
  same knob, never hand-set: a pane sits inside the frame already, so it takes half the
  inset and re-tunes automatically when the gutter does.
- Inside a `Section`: `space-y-3` (it provides it). Forms: `space-y-4`; tight groups: `space-y-2`.
- List rows: `px-4 py-3`. Card padding comes from `Card` (`--card-spacing`); don't re-pad.
- Grids: `gap-3` (cards), `gap-4` (form fields), `gap-2`/`gap-1.5` inline.

## List pages (the checklist every new tab copies)

/tasks and /projects are the two speakers; a new list page is built by walking this list,
not by taste:

1. **No in-page title or explanation** — the breadcrumb is the page's name.
2. **Standing controls ride the topbar**, in this order: search (`w-56`) · Filter
   (icon-only funnel; widens with a count when applied) · Display (icon-only sliders:
   layout + group-by) · "+ New …". A page adopts ONLY the controls its wire supports —
   never fake a dimension the API can't filter (projects ship Display without Filter until
   the wire grows a role param).
3. **In-page, `SectionTab`** — which renders its `min-h-8` row only when given content:
   active-filter chips, or the selection bar. Never a standing band of dead air.
4. **State is URL-borne** where a slice is shareable (`?q= &group= &status= …`), parsed in
   one place; embedded copies (a project's tab) go component-local.
5. **Lists** are `Rows` of `px-4 py-3` rows: one leading mark from the shared vocabularies
   (`StatusMark`, `ProjectIcon`), one truncating name, meta right; grouped views are kit
   `Section`s with PLAIN counts (window caveat in the tooltip; `LoadMore` is the visible
   more-signal); keyset pagination only.
6. **Bulk selection** (where verbs exist): the leading mark swaps to a checkbox on hover,
   the control row becomes the selection bar (n selected · verbs · clear), ⌘A/Escape,
   per-item verbs via allSettled with an honest partial-failure toast.
7. Empty states via `EmptyState`; the search-miss variant names the query.

## Sidebar sections (the left rail's list pattern)

Starting a task is a VERB that belongs to Tasks, never its own nav row (a row reads as a
place, and Home already is that place) and never in the header (beside the brand and the
workspace switcher it reads as a workspace setting): a `SquarePen` **New task** action at the
right end of the Tasks nav row (`SidebarMenuAction`, always shown), and, since the icon rail
hides row actions, a ghost pen under the logo there. It links to Home's composer
(`NEW_TASK_HREF` in `lib/links.ts`) and its tooltip names the chord (⌘⇧O,
`lib/new-task-shortcut.ts`). Never on a page's top bar, where it would read as one of that
page's actions (Share, fork).

The head of the sidebar is the workspace switcher, a CARD (bordered, `bg-card`), never a nav
row: Propel's `WorkspaceAvatar` (the workspace's initials on its own colour), the name, your
role, a chevron. Its menu lists each membership as a card of the same anatomy under a
"Workspaces" label, the check on the one you are in, then Create and Join.

The nav is two runs (`NavRun`): where the work is (Home, Inbox, Tasks, Projects, Routines) and,
under a quiet "Set up" label, what the work is set up with (Bots, Connectors, Allowed sites).
A list section never repeats a nav row's name: the recent tasks are "Recent tasks".

Every list the sidebar carries — Projects, Pinned, Recents, and whatever comes next — is ONE
anatomy (`sidebar-projects.tsx` and `sidebar-recent-tasks.tsx` are the speakers):

- `SidebarGroup` + uppercase `SidebarGroupLabel`, the whole group hidden on the icon rail
  (`group-data-[collapsible=icon]:hidden`) — the rail is primary nav only.
- Rows are `SidebarMenuButton` (the app.css bridge sizes them: 36px, ~13.8px labels): a
  LEADING mark in the icon slot — the identity or state vocabulary, never invented per
  section (`ProjectIcon size="sm"` for projects, `StatusMark` for tasks) — then ONE truncating
  name. No second line, no meta columns; the section's own page carries detail.
- Active is `isActive` by exact path; row actions ride `SidebarMenuAction` on hover (the
  recents' ⋯ is the reference), never inline buttons.
- Sections show a WINDOW (≈5–20 rows; Projects caps at 5), seeded best-effort by the shell
  loader (a hiccup must not blank the app) and fresh on navigation. When the window
  overflows, the section MAY close with one quiet muted "View all …" tail row — a LINK to
  the section's page, never an in-rail expander; the rail must not bloat.
- A section MAY carry ONE quiet options control beside its label (`SidebarGroupAction` +
  a menu — the Tasks section's sliders is the reference): DISPLAY prefs only (ordering,
  mark visibility), persisted in STORAGE, never data verbs — those stay on the rows' ⋯.

## Radius (Plane's scale)

Every corner is a step of **Plane's scale**: Tailwind's default `rounded-*` steps, which Propel
keeps (ADR-074; the hand-set `--radius` knob retired with Prismoid's own look). Nothing else may
state a radius.

- **Steps are roles**: `xs` grips/kbd/micro-chrome · `sm` inline code + calendar chips ·
  `md` controls (buttons, inputs, selects, menu items, tiles, board cards) · `lg` surfaces
  (cards, rows, popovers, board columns, segmented racks) · `xl` page-level objects
  (dialogs, command, artifact/collection shells, docked bars). Concentric nesting steps
  down: column `lg` → card `md`.
- **`rounded-pill`** is the text-chip radius — `Pill`, `ToggleChip`, `StatusBadge`, filter/view
  chips, count bubbles (Propel's `Badge` keeps Propel's own corner). Chips are control-round (`--radius-pill` = `md`), never stadium.
- **`rounded-full` means a circle** (or a track): dots, avatars, radio, switch, spinner,
  progress meters, sub-12px gantt lozenges. If it carries text on
  one line, it is a pill, not a circle.
- **The prompt box is the one `2xl`** (2026-11-18, owner: match Plane's AI chat input): `@prismoid/blocks/prompt-input` wears a 16px corner, the
  raised shadow, no hover shift and a focus that only strengthens the border (no ring), on
  Home, the task follow-up bar and bot chat alike. Its ↑ send is a control-corner (`md`)
  square, as Plane's, no longer a disc.
- The bare and arbitrary radius utilities skip the scale and are **banned** in the app
  (`lint:radius`, part of `web-check`).
- **/ds** is the living gallery: tokens (with the radius scale), kit, Propel,
  blocks, renderers, and the full component census. Registered in the app map, linked from
  nowhere — an easter egg by design. Drift is meant to be visible there.

## Hairlines (the "unwanted lines" law)

- **Surfaces** (cards, lists, panels): `ring-1 ring-foreground/10` — that's `Card` and `Rows`.
  Never `border` a container by hand; never `divide-y` outside `Rows`.
- **Chrome edges** (shell header): `border-b` — at most one rule at the top of the viewport.
  Nothing inside content draws parallel rules near it. The composer bar is RULELESS
  (2026-08-02): the input box's own ring is its edge — a full-width line above a floating
  input reads as a wall; whitespace separates.
  A rule must have something to separate: Home's header goes `border-transparent` (open canvas
  beneath — same 1px so routes don't shift).
- **Frame rails** (the task page's Files rail, the settings nav): the `surface-frame` class
  (app.css), never a hand-typed `bg-sidebar/...` wash — light wears the sidebar's whisper
  tint, dark is hairline-only (the one-surface law).
- **Mid-content rules**: only `LabeledDivider` (a rule with words in it). No `before:h-px` tricks.
- Interactive surface affordance: `hover:ring-foreground/25 transition-shadow` — never
  `hover:border-*`.

## Color

- Semantic tokens only (`lint:tokens` enforces no raw palette anywhere in `app/` or `packages/blocks/src` outside `app/app.css`).
- **Contrast is a gate, not a taste** — `pnpm lint:contrast` (part of `web-check`) resolves the
  tokens through the stylesheets that define them (Propel's variables, `@prismoid/blocks`' accent,
  app.css) and asserts WCAG AA on every fg/surface pair the app actually renders:
  ≥ 4.5:1 for text (tones and hues are measured **on their wash**, alpha-composited), ≥ 3:1
  for UI (ring, primary control). Body-text pairs additionally report AAA (7:1) — today every
  body pair passes AAA in both schemes. Retune any token freely; the gate decides.
- **Plane's surfaces, Prismoid's names** (ADR-074): app.css points each semantic token at Propel's,
  and Propel switches its own values per theme (`data-theme`, light | dark).
  - The rail is the canvas (`--sidebar` = `bg-canvas`, Plane's grey), the work sits on the
    white surface (`--background` = `bg-surface-1`), and objects sit on `bg-layer-2`
    (`--card`/`--popover`). Muted and secondary fills are `bg-layer-1`.
  - Dark climbs the same way: canvas < surface < layer, all Propel's neutrals.
- **The accent is monochrome**: black in light, white in dark (`@prismoid/blocks` points Propel's
  brand ramp at its neutrals). Primary buttons, checks, switches, selected tabs and focus rings
  follow it; a primary label is `text-inverse`, so it flips with the fill.
- Status tints come from **`kit/tone.ts`** — border `/25`, wash `/10`, text full. Never retype
  alphas; never invent a new alpha for an existing tone.
- **Categorical hues** (`--hue-1…8`): decorative kind-colors for user data — select options,
  chart series. The recipe is `kit/tone.ts` `HUE_TINT`: wash `/10`, text full strength, border
  transparent — one alpha, both schemes (`/15` fails AA in dark: a self-wash brightens the
  backdrop as fast as the text, capping the ratio ≈ 4.3). Never for status (status stays
  `TONE_TINT`), never hand-picked: a value's hue is its option index `% 8`, deterministic
  and stable.
- **Pill vs Badge**: names a _state or verdict_ → `Pill`/`StatusBadge`/`VerdictBadge` (12px).
  Names a _kind, role, family, version_ → ui `<Badge>` (10px).

## Elevation

Three steps. Shadows join rings, never replace them.

- **rest** — an artifact-object standing on the page wears `bg-card` and carries `shadow-raised-100`
  (the collection shell, the verification disclosure) — never `bg-background`: an object the
  color of the canvas gets no lift in dark. Sticky occluders inside it (table header, gantt
  rail, footers) match the object's surface. Content surfaces _inside_ an object — cards,
  buckets, rows — carry no shadow: the ring is the edge.
- **raised** — lifted or docked over content: `shadow-raised-300` (a board card mid-drag, the record
  peek, the dirty pill).
- **overlay** — Propel's menus, popovers, selects, dialogs and toasts carry Propel's own
  `shadow-overlay-*` steps. Prismoid's own floating cards (the task page's docks and strips) ride
  `--shadow-overlay` (app.css), scheme-tuned **by design**: dark shadows are much heavier — a
  light-calibrated shadow vanishes on graphite and the surface "interferes" with the plane
  beneath. Tooltips stay shadowless (the inverse chip is its own contrast).

## Selection

One accent — `primary` — two grammars:

- **Floating objects** (board card, gallery tile, calendar chip, gantt bar):
  `ring-2 ring-primary/40`, _replacing_ the rest ring (keep `ring-inset` wherever the rest ring
  is inset). Selection changes the ring only, never the shadow.
- **Flow rows** (table row, list row, gantt rail + strip): `bg-muted` plus a left accent edge
  `shadow-[inset_2px_0_0_0_var(--color-primary)]` on the row's _leading_ cell (an inset shadow
  shifts no layout and travels with sticky cells). Hover stays one step lighter
  (`bg-muted/40`) and is never applied to a selected row.

Propel's `<TableRow selected>` paints the selected fill; the accent edge is added by the
consuming cell (the collection's own table lens paints both).

## Motion

- State changes animate color and shadow only:
  `transition-[color,background-color,box-shadow] duration-150` (default easing). Drop `color`
  from the list when no text changes.
- Never transition layout — widths, heights, positions. Resizable panels track the pointer
  live; panels and columns mount instantly. Registry open/close animations stand as shipped.
- Color/shadow fades are not movement: no `motion-reduce` fork needed for them.
- **Liveness is the one looping exception**, and the mark is the FADE TRIO (kit
  `Liveness`, 2026-08-29 round two - the city block shipped and was hated in a day, on
  the record): three dots rippling by OPACITY alone, no bounce, so the mark sits calm
  beside tabular figures - never the stock rotating arc (the arc `@prismoid/blocks/spinner` stays
  for the composer only). It IS `StatusMark`'s
  working glyph (a mark standing for a _category_ over an empty set - a board column with
  0 tasks - takes `still`: motion must have something to report), and reduced motion falls
  still at readable opacity, never invisible. `StatusDot`'s rippling halo
  (`animate-status-ping`, the step timeline's micro mark), and the stream cursor — nothing
  else loops. The MARK is the status vocabulary for rows/columns/topbar (glyphs, not
  colored dots: spinner = working · speech bubble = needs input · clock = queued · check/
  X/ban = settled); both marks fall still under `motion-reduce` with tone intact. The topbar
  mark of a task waiting on a person says it in words ("Needs you", `StatusMark labelled`), and
  a step of that task shows the waiting dot, never running.

## States

- Loading: `LoadingState` (spinner + sentence). Nothing pulses except live-status dots.
- A whole page on its way: `PageLoading` (kit `brand.tsx`: the `BrandMark` tile breathing by
  opacity over one sentence; still under reduced motion). Two sites only: the first paint
  (`root.tsx` `HydrateFallback`) and a change of screen. The router holds the old page until the
  new one's loaders settle, so the shell swaps the page area for `PageLoading` once a change of
  pathname has been loading for 120 ms, and the sidebar lights the destination on the click
  (`hooks/use-pending-path.ts` over `lib/pending-route.ts`). A query-string change, a
  revalidation or a submit keeps the page on screen. Inside a page, loading stays `LoadingState`.
  Home wears it a third time: on Enter the landing swaps to the ask (a `UserBubble`) over
  `PageLoading` ("Starting your task…") while the task is created; the composer stays mounted,
  hidden, and returns with its draft when the create fails.
- Dictation is visibly on: a `VoiceMeter` (12 bars scaled by the microphone's spectrum,
  transform only, at rest under reduced motion) sits beside the mic, which becomes a stop
  square. Both wear the mic's own colour, never red: the moving bars say "recording". The
  words go straight into the box as they are heard, at the end of the text, and a recogniser
  error is a toast naming its cause.
- The task-minting box's bottom row rests at five controls: "+", the Project picker, **Options**,
  mic, send. Options (`composer/options-menu.tsx`) is one menu whose rows name a setting and its
  current value (When, Mode, Size, Deliverable, Fleet models, Clarifying questions, Budget,
  Skills, Connectors, Bot) and open that setting's choices as a submenu beside it. Past Options
  the row holds only what was changed: a setting off its default wears its own pill (a click
  edits it in place) with a clear button beside it. A value that needs typing (a custom
  schedule, a custom amount) opens that setting's panel from the row, never a field in a menu.
- Absence: `EmptyState` (`compact` inside sections). Never a bare muted `<p>`.
- Form failure: `FormError`. Toggles/segmented choices: `ToggleChip`. Icon wells: `IconTile`.
- **The composer continues the task; forking is the only branch** (v5, 2026-08-06 —
  ADR-018; v3/v4's follow-up-as-fresh-task is replaced: it minted a new row + URL + empty
  page, and users read it as "the context reset to zero"): a settled task's box sends the
  next TURN of the SAME task — an optimistic right-aligned bubble lands instantly, the
  durable `task.continued` frame reconciles it by the send token, status re-enters
  `planning`, the current plan rolls into the rail's history, and the page NEVER navigates.
  The SSE socket is never closed on a terminal frame — a settled task is a conversation at
  rest, and its next turn rides the same stream. _Fork this task_ (⋯ menu) stays the one
  click that mints a NEW task: it carries the whole conversation and the parent's dials,
  wears "[Forked] <label>", and is born HELD (status `created` + `held_at`, sweeper skips
  it) — it sits with the composer ("Where should this branch go?") and the first directive
  clears the hold and launches; a fork page renders its parent thread above a "forked from
  <parent goal>" divider (the name links to the parent) and, while held, a status line that
  says "Held", that nothing has started, and what starts it; the box shows no Stop, since
  nothing runs (the ⋯ menu's Cancel still discards the branch). The thread is the task; forks are its branches. Robustness rides the wire:
  create/follow_up/fork take a `client_token` (one per send gesture, `crypto.randomUUID()`),
  and a replayed token returns the existing task or turn — double-fires, retries, and
  script reruns can never mint twins.
- **The end of a response is visible** (ADR-019, 2026-08-07): a settled turn renders the
  action row under the answer — kit `CopyButton`, thumbs up/down (ghost `size-7` icon
  buttons, `aria-pressed`, chosen = `text-success`/`text-destructive`, durable
  `task.feedback` frames with the newest winning and a reset per turn), and _Retry_ (ghost
  `sm`, RotateCcw + label — runs the latest ask again as a new turn on the same page). The
  row folds away the moment the next turn opens. Verification arrives AFTER delivery now:
  artifact cards wear the muted "Pending" pill until the `verification.signed` frame lands
  on the open socket, and the reader says "Not verified yet. The receipt shows here once the
  check finishes." A FAILED receipt never turns "Delivered" into anything else (delivery
  happened): the epilogue puts an amber caveat line above it, tied to it by an amber rule, with
  a link that opens the artifact, the reader repeats the note above the content (its link opens the receipt),
  and the spend that landed after delivery is named on the Delivered line ("$0.37 to check")
  and in the Models popover ("Verification (included above)"). The receipt names its judge,
  or says "No model judge" when none answered.
- **Questions dock at the composer** (2026-08-04): the task page stages a gate ONE question
  at a time in a card directly above the box — the prompt IS the title, ‹ n of m › stepper,
  numbered option rows (a direct reply may type the number), a pencil free-row for
  text/`allow_other`; _Skip_ = decline, ✕ collapses to a "N questions waiting" pill.
  The composer beneath never dies — "Or reply directly…" routes into the current question,
  and with several questions the placeholder teaches the batch grammar: `"1: …  2: …"`
  answers them ALL in one send (segments split on newlines/semicolons/inline `2.`
  boundaries; a fragment picks an option by number or unique label match, else it's the
  free answer). Answering the LAST question submits the batch, and the thread keeps the
  **qa record
  card** — prompt in medium ink, the answer quiet beneath, option labels never wire
  values. The inbox keeps the all-at-once `GateCard` (a pane answers in place; a stepper
  would fight its scroll).
- **Capability**: a menu offers only what the current context can honor — an option that
  can't apply (wrong layout, unsupported data) is **absent**, never disabled with an excuse
  ("— list only"). First ask whether it _should_ work there (Date/Month group boards fine —
  buckets are buckets); only then hide. `disabled` is reserved for real states with a real
  path out: lifecycle guards (a live task's Archive — cancel first) and within-control
  invariants (the last visible field). The collection GroupMenu is the reference
  implementation (board omits None; sub-group exists only on board).

## The task page's right side (v1, 2026-08-07)

Two surfaces may own the right edge. **Never both — one surface at a time is the law**, and
it is enforced structurally in `routes/tasks.$id/route.tsx` (the reader's mount excludes the
rail's), not by remembering.

**The component map** — three files, three responsibilities, zero overlap:

| file                        | owns                                                                                         | must NOT own                                     |
| --------------------------- | -------------------------------------------------------------------------------------------- | ------------------------------------------------ |
| `tasks.$id/task-rail.tsx`   | the CONTENT: Progress · Outputs · Sources · Usage, each a `RailSection`                      | chrome, width, scroll, fetches, open/close state |
| `tasks.$id/reader-pane.tsx` | the SPLIT pane: width + drag, Escape, the header cluster, `PaneProgress` popover shell       | artifact state, auto-open, the rail hand-off     |
| `tasks.$id/route.tsx`       | ORCHESTRATION: who is open, who closes whom, the dismissal latch, `railProps` assembled once | any section markup                               |

**The rail** is a floating card (`bg-popover ring-foreground/10 rounded-xl
shadow-(--shadow-overlay)`, `w-80`, inset `my-3 mr-3` — the QuestionDock's floating grammar,
deliberately). Its four sections, always in this order: **Progress** (live plan: "n of m" and
the liveness mark on the title's own row · quiet bar · `StepTimeline`, whose dots share one
fixed column with the thread running dot to dot and titles wrapping to two lines; earlier plans
behind one disclosure, on the same left edge, drawn `settled`: still, quiet, a step that was in
motion reads "replaced") · **Outputs** (published
artifacts; click opens the reader) · **Sources** (the task's attached inputs; click
downloads; hidden when empty) · **Usage** (live spend + calls off the ledger, the context
lanes verbatim; collapsed by default). One content component, **three homes**: the floating
card (wide), the overlay Sheet (narrow), and the reader's Progress ▾ popover — `railProps`
is assembled once in the route so no mount can drift.

**The reader — ARTIFACT MODE** (v4, 2026-08-07; the iteration's lesson in one line: v1
crushed the chat, v2 orphaned the site nav, v3 tucked the artifact under the shared bar —
v4 keeps all three truths): **all three regions stay visible together — the site nav, the
conversation, and the artifact — and the artifact gets its OWN PLANE, from the very top
of the window.** The shared topbar **yields** (`TopbarYield`, `layout/topbar-slot.tsx`:
the shell header steps aside while mounted); the sidebar stays; the row owns the full
viewport height, and each pane wears its own header on the same h-14 line:

- **chat side**: its own band — `← back · title · status dot · spend/calls · Progress ·
  Files · ⋯` (ONE `taskChrome` cluster, two homes: the shell topbar normally, this band
  in artifact mode — no control ever vanishes, fork stays one click away);
- **artifact side**: a PINNED band (`shrink-0`, outside the scroller) — `✕ close ·
  ⤢ full screen · ⬇ download · title (truncated) · verdict` — because the scroller pans
  horizontally under wide content and controls inside it rode the pan clean out of the
  window (the close first, then Download). The download link rides ArtifactView's one
  fetch (`onDownloadInfo`, always the viewed version; `showDownload={false}` suppresses
  the in-row copy); the verdict rides the route's live shelf, so the badge flips the
  moment the receipt frame lands, and the pane hands it to ArtifactView (`verdict`), whose
  receipt re-reads on a new one instead of keeping "Not verified yet". The pane renders ArtifactView with `chrome="bare"`:
  no PageHeader block inside the content — the band IS the identity.

**ONE vertical scroll — the page's/pane's, for every kind** (2026-08-07, twice): a
collection flows at FULL natural height exactly like a document — the ViewShell's
viewport cap is gone, the board reads top-to-bottom under the page scroll, and the
shell's internal scroller only ever pans wide layouts HORIZONTALLY (the pane never
scrolls sideways law needs that box). A board bounded to its host scrolled inside
itself — named ugly twice, from both directions. The registry's `frame:"app"` contract
survives for genuinely app-like kinds (a webapp preview); no shipped kind uses it, and
`registry.test.tsx` pins that. The record peek bounds ITSELF in document flow
(`max-h-[80dvh] self-start`) so a long record never becomes a panel as tall as the
board.

The **Progress button** opens the rail as a centered **modal** (Dialog, same TaskRail
content; picking an output there swaps the reader) — the rail yielded the right edge, so
it escalates rather than floats.

**EVERY KIND SHOWS ITSELF (ADR-022).** The render sidecar (`preview-view.tsx`, one fetch,
one switch on `preview.kind`) mounts in the no-renderer branch above the DownloadCard
floor — gated on `has_rendition || has_preview`, because the two are INDEPENDENT
best-effort legs: sheet grids with COMPUTED values, document blocks with real heading
structure, deck slides as quiet cards, code files as monospace source. A PDF is the one
kind whose bytes ARE the preview — `RenderHint.PDF` → `RenditionView`
(`rendition-view.tsx`), the browser's own viewer over `/rendition`'s inline presign, the
DownloadCard as the `<object>`'s literal fallback child AND as the degraded floor when
the fetch fails (a registered renderer has no shell floor beneath it — it carries its
own). Both self-bound in document flow (`h-[75dvh] max-h-[56rem]`, the record-peek
precedent) — a plugin's or a page's INTERNAL scroll is its own; the app never nests a
CSS scroller. THE PAGE CAGE is law, pinned by `preview-view.test.tsx`: a `page` preview
iframes with `sandbox="allow-scripts"` and NOTHING else — never `allow-same-origin` —
plus `referrerPolicy="no-referrer"` and the no-network CSP meta injected at the
DOCUMENT FRONT (ahead of anything parseable — a head-anchored meta arrives too late for
content smuggled before `<head>`); loosening any of these is a same-origin escape, not
a styling choice. The DownloadCard stays under every preview and its copy sells the
download ("open in Excel/Word"), never a false absence.

**The chat takes the maximum area, and GRID arbitrates.** The artifact-mode row is a CSS
grid: `minmax(480px, 1fr) minmax(400px, <pct>%)` (`splitColumns`, reader-pane.tsx). Two
predecessors earned this: JS-measured pixels lagged every width change they hadn't
subscribed to, and a flex row let the pane's %-clamp width and the chat's min-width fight
independently — the chat crushed to zero while the pane painted past its ceiling (caught
live in devtools). Grid resolves both tracks TOGETHER; neither floor can be violated and
overflow is structurally impossible. The share persists (`STORAGE.splitPct`, never the
overlay drawer's key); defaults are compact (40% prose / 52% board-grid); mid-drag writes
the template straight onto the row (zero transcript re-renders), committing on release.

**One line across the split.** Both headers in artifact mode are exactly `h-14` with a
`border-b` — the chat band and the pane band share one continuous horizontal rule. A
mismatched rule under the headers reads as breakage, not design.

**The nav edge is honest** (`NavResizer`, `layout/app-sidebar.tsx`): the sidebar rail's
cursor always promised `col-resize` — now drag resizes (208–400px, persisted to
`STORAGE.navWidth`, written straight to `--sidebar-width` so every layout reading the var
— the inset, the split's clamp — re-solves in the same frame), a plain click still
toggles collapse, arrow keys nudge. Collapsed or mobile falls back to the plain toggle
rail.

**The chat has a FLOOR**: `CHAT_MIN_PX` (480px, `reader-pane.tsx`) — every pane width,
saved or dragged, clamps against the CONTAINER (the shell inset, not the window — the
sidebar owns real width) minus that floor, and the task column mirrors it as
`min-w-[30rem]`. The pane pushes; it never crushes. Drag the pane's left edge to resize
(width preference shared with the overlay reader); Escape closes. Narrow screens keep the
overlay Sheet (`components/artifact/artifact-reader.tsx`), which inbox/projects/ds also use
— the split is task-page-only. (Edit landed with ADR-033 as a RENDERER-owned affordance —
the collection precedent: text documents grow it inside `renderers/document-edit/`, the
shell stays fetch/boundary/floor-only, and the history lives in ArtifactView so all four
hosts share it. Share stays a recorded want — add it to ArtifactView's row when it lands,
never as a dead button.)

**The interaction laws** (all live at the route's right-side state block):

1. Opening the reader — from a card, the rail, or a live publish — hides the rail. Closing
   it restores the rail to its saved preference, which never moved.
2. While reading, the rail's content is one click away behind the topbar's **Progress**
   button, as a centered modal. Same element, third home.
3. **A publish surfaces the artifact in place**: while the task is live, a new artifact or
   revision opens the pane so the change is seen, not discovered. A page load only sets the
   baseline (a settled task's replay never flings the pane open).
4. **The dismissal latch**: a manual close means "stop showing me this one" — auto-open
   respects it until a _different_ artifact (or a new revision) publishes.
5. The topbar Files toggle while reading = "give me the rail back": one gesture closes the
   pane and shows the rail.
6. Every right-side mutation goes through `withTailPin` — the transcript must only ever
   slide sideways, never settle vertically.

## The execution grammar (the progress tree, 2026-08-07; phase-split 2026-08-30)

One `execution` FeedItem per PHASE holds a stretch of machinery as `groups[]` (round six,
owner: "it needs to come in midway"). The phase law lives in `withExec2`: a frame whose
step group already lives in one of this turn's trees folds THERE (ownership: a parallel
wave stays one tree even while siblings' output lands below it); a NEW phase opens at
STEP BOUNDARIES ONLY — a step's first durable frame arriving after content (a narration,
a card, a chip row) starts a new tree at the tail. Nothing else may mint: the live lane
pre-seeds groups from ephemeral activity frames replay never sees, so a content-triggered
mint on any other frame kind splits the lanes (the tree-parity law caught it live).
Settling settles EVERY phase tree. The transcript reads work → output → work → output.
Moved IN: the plan (a group whose checklist is the plan's
own steps, ticking), retry findings (warning notes inside their step's group), and GATES
(a wait row — "Waiting on you — N questions" → "You answered — 2m 10s" / "Skipped N
questions — proceeding with defaults" + a note NAMING the skipped prompts; four intake
rounds once stacked eight loose amber lines over a settled answer). Stays OUT: user/qa
bubbles, segments, artifact cards, the escalation card, the epilogue (delivered/
verification/memory consolidate there; a fleet's mid-run receipt keeps a quiet amber
marker). One item = one MessageScrollerItem = one content-visibility boundary.

**Working artifacts are CHIPS, not cards** (round six): a `working`-role non-image
(a section file, a seat's records) upserts into a trailing `working-artifacts` chip row —
consecutive publishes fold into one wrap row, deduped by artifact id, each chip a quiet
`bg-card ring-1` button opening the reader. Deliverables keep full cards; working images
keep their figure grid. Backed by the quiet lanes upstream: `section`/`harmonize` steps
publish WITHOUT a narration (the tree row + the chip already carry the receipt), so the
thread never reads "Published 03-….md" a dozen times between two paragraphs.

**Settled = one quiet line** ("Completed 7 steps ▸" — issues AND skips counted honestly:
a skipped step is named in the root, never absorbed into "Completed", and wears a muted
minus in the checklist, never the success check; a turn that ended on an issue says how far
it got against the plan's own step count, "0 of 2 steps done · 1 issue", and never
"Completed", amended 2026-11-12), expanding to the full tree. **Live = root open, the most-recently-active group open, finished
groups folded** — user clicks always win (component-local overrides, never persisted).
Inside a group: checklist → acceptance list → rows (tool/round/note), clamped at six
behind "+N more" (render clamp only; the reducer keeps everything). Live groups anchor
the visible window to the tail.

The rows speak `progress-copy.ts` — a pure data table (verb/icon/tone per tool; new tool
kinds are DATA rows, never components; unknown tools render their raw name with a wrench
and no arguments, ever; a keys-only live label ("args: url") is never shown on any row, and a
known verb with no locator yet reads its own object, "Opening a page"). A call that did not
succeed is named for what happened, never "Denied" (only a person declines, and that speaks on
its gate row): "Failed" with the reason minus the exception's class name, "Blocked" for the
egress proxy's refusal, "Not allowed" for a tool the step was not granted (2026-11-12). Machinery rows (engine/phase facts) speak whole sentences with
no verb prefix. Durations speak ONE scale law — `~/lib/time`'s `formatSeconds`, human
units all the way up with DAYS as the ceiling ("45s", "1m 30s", "2h 1m 15s",
"1d 2h 5m 30s" — never weeks or months; user-locked 2026-08-08, retiring the one-unit
"247s" rule): settled facts under 10s keep one decimal ("3.4s" — micro tool calls are
where tenths carry signal); live clocks tick whole seconds; never "4:07" (colon clocks
read as timestamps).

**The streaming deliverable is never opaque**: a prose draft pours through Markdown (the
document composes in front of you); a structured `.json` draft renders the live
BREAKDOWN (`draft-shape.ts` decides, `draft-card.tsx` renders — never JSON.parse on a
partial body, every value lands only when its closing quote does, and the stream is
SPLIT at `"rows":` so top-level facts read from the head and row facts from the tail —
a collection ABOUT titles can never headline the card, and a stray key in a row can
never mint a phantom column or view). It fills top-down at the stream's own schema
order: the organizer's REAL file name replaces the scratch "draft" name the moment the
first key closes ("launch-tracker.collection.json"), the title headlines, the one-line
summary says what is being built before any row exists, the columns land one by one
("2 columns — Name · Stage"), the row counter climbs with the visible TAIL of the last
few rows — each row named by its first STRING cell, so a rank-first table keeps its
heartbeat — newest arrowed ("→ Q3 launch email"), the views arrive last (bounded to the
real layout enum). "Building — N rows" alone answers nothing while a complex artifact
is born (user, 2026-08-08).
Liveness inside the tree is ONLY StatusDot ping / Spinner / shimmer / the live elapsed
clock (LiveTicker's precedent: a ticking number is sanctioned liveness) — and NOTHING
claims liveness once the item settles: a cancel mid-step leaves `running` states and
pending rows behind, and the settled snapshot renders them still (their words kept,
their motion gone). Collapse animates the chevron transform, never height. The indent
rail is the sanctioned hand-border — `border-l` + `pl-4` in the tree; StepTimeline draws
the same rail as a `w-px bg-border` strut. Denied rows are the tree's few loud lines
(warning tone, reason attached).

**Lane parity is the tree's constitution**: live rows arrive via ephemeral `activity`
frames (tool starts with pending spinners, model rounds "Thinking… → Thought for Ns",
streamed thinking text, engine/phase facts) and reconcile with durable frames by
`tool_call_id` — a refresh mid-run rebuilds the IDENTICAL tree from durable frames alone,
minus the live-only row classes, named in full: pending flags, streamed thinking text,
model-round rows, and engine/phase machinery rows (`context.assembled`'s is the one
machinery row that IS durable). The same sanctioned delta as `segment.live`.
`treeShape()` in task-view.test.ts pins it, with `activity` frames in the live fixture
so the delta is exercised, never vacuous.

**Identity is first-class**: every group wears its agent as a Badge chip on the trigger
(visible folded or open, never truncating), tinted from the categorical ramp by the
select-option index law (a stable name hash, so the same agent reads the same everywhere;
identity, never status) and set as a SYSTEM TAG - `font-mono tracking-wide uppercase`,
caps by text-transform only so the DOM keeps the lowercase registry id (screen readers,
copy), tracking-wide never -wider (long user-bot names must not shout); machinery rows name their actor ("Clarifier ·
Reviewing your request"); the root line carries the cast (settled counts distinct agents,
live names the worker, a fan-out reads "N agents in parallel"). Before any step is
active, the plan group is the auto-open one — intake and planning are never invisible.

**The trigger is TWO TIERS** (2026-08-29, the owner's screenshot: a single line made the
model ref fight the title for width, titles truncated mid-word while
"grok-4.20-non-reasoning" repeated at full length and nothing aligned). Line 1 is the
story: state mark · title (flexes, truncates last) · agent chip · chevron. Line 2 is the
receipt, aligned under the title: model ref in quiet mono (full value in the tooltip),
then attempts · calls · tokens · cost · duration in tabular figures. Every datum stays;
a group with no receipt yet stays one line. Step titles carry the ROLE ("Map the
territory", "Gather core-dedicated"), never a restated goal: the goal is the page's own
user bubble.

**The epilogue** (same day; receipt 2026-08-30): the end of a turn is ONE quiet
sentence-shaped block — "Delivered in 6m 32s · 14 steps · 4 agents · 812.4k tokens",
the verdict on its own line above it (the verdict rule, below), memories elaborated as full indented
lines ("preference · Prefers tables over prose."), never a stack of colored markers.
The receipt is the TURN'S CONCLUSION (round seven, owner: "solve for the breakdown,
but not conclusion"): the phase split gave each tree its own slice, so the epilogue
sums them — steps and cast counted, tokens summed from the groups' ledgers, duration
as wall clock first-anchor→last-anchor (gate waits included: total time is what the
user lived through). Raw values ride the reducer (`turnReceipt`, stamped at
`task.delivered`); renderers format (`receiptBits` — the tail line's grammar at rest).
A legacy replay with no anchors shows no receipt, never zeros. Cost stays in the
topbar — it owns money. **The countable bits are X-RAYS** (same day, the hover ask):
"14 steps" and "4 agents" wear a dotted underline — the whole affordance, quiet — and
open a HoverCard (the vendored preview-card's first product use, 150ms open): steps →
one row per step in feed order (title · AGENT mono-caps · time · tokens; a skip or an
issue is named in muted text — the delivered line stays monochrome), capped at 16
behind "+N more" (render clamp only); agents → the cast rollup (AGENT · N steps ·
tokens; a planner's row, present only when a planner drafted the plan, reads "planning" — its
work is the phase; a plan the Lead or a recipe made names no planner, so a one-call reply is
one agent, not two). Each turn's receipt totals that turn alone, live and after a reload alike
(the view always starts at turn 1 and `task.continued` carries the rest). Time and tokens
stay plain facts: their breakdown lives inside the cards. The reducer carries the raw
`rows` on the receipt; `receiptStepRows`/`receiptCast` are the pure builders (portal
content never reaches static markup, so the pins hold on the builders). The peek's
epilogue stays the slim one-liner (capability law). **The verdict rule** (2026-11-18, owner's
pick among shown options; it replaces "monochrome, one amber accent"): what the check said is
its own line ABOVE the delivery sentence, and a 2px rule down the side ties the two, coloured
by the verdict - amber for a failed receipt ("Checks found problems. Read it with care.", a
`note`) or one nothing could examine, green for a clean one ("Verified · suite · judged by …").
A turn with no receipt (nothing to check, or the receipt not landed yet) wears no rule and no
verdict line: the stream does not say whether a receipt is owed, so the block never claims one
is coming. "See the receipt" rides the verdict line. The
sentence itself reads "Delivered in 33s · planned · 2 steps · 2 agents · 51.7k tokens · $0.08
to check": the time joins the word, and tokens are ONE total whose x-ray is the fresh and
cached split. `epilogue-verdict.ts` decides, `epilogue.tsx` draws. Never red here (the
red verdict lives on the artifact badge, where you act on it). Verification and memory
frames fold INTO the block as they arrive (`epilogueId`); a fleet seat verifying mid-run
(no epilogue yet) keeps a quiet amber marker. Tree rows breathe: `space-y-1.5` +
per-row `py-0.5`, notes and "+N more" align to the text column (`pl-[22px]`) so every
panel reads as one left edge.

**ONE liveness home - the tail** (2026-08-29, the owner's three-spinners screenshot):
while a task is busy the topbar's StatusMark HIDES (`statusSpins` gates it; the ticking
spend counter is passive proof of life; settled/parked marks return with their vocabulary),
the "working · Ns" title band is GONE (removed with its jobs, not thrown away: the clock
lives on the tree root, the "waiting for signal…"/"reconnecting…" truth-strings ride the
same tail row as `note`), and the transcript's LAST line is the one
breathing thing: **the tail line** - `mark · time · tokens · what it's doing` (the
owner's grammar, 2026-08-29). It is mounted at ROUTE level as the transcript's TRUE
bottom (round five: a streaming draft card renders below the tree, and a tree-owned tail
was stranded above it) - groups, then the draft card, then the tail, always; while a
draft pours the phrase names the file being written ("Drafting launch-memo.md"), and the
draft card's own header mark is the same trio, micro-sized. The tail is PURE STATUS - no chevron, no button (round three: a status line that
secretly collapses content ABOVE itself inverts every disclosure in the app). Folding
lives where it always worked: finished groups auto-fold, any group folds by its own row,
and the settled "Completed N steps ▸" header is the one root collapse surface. Only the
DOING phrase is an aria-live region - a ticking clock in one would spam screen readers.
The pre-tree WorkingLine speaks the same grammar anchored on the task's birth. Never two spinners on screen for
one task.

The rail keeps the step triplet (dot·title·attempt); the tree owns intra-step detail.
WorkingLine survives only for the pre-tree stretch (created/clarifying, legacy replays);
the active group carries the no-dead-air shimmer itself. Kit `QuietDisclosure` + the
route's `progress-tree.tsx` are the product's step and tool disclosure (the vendored
`ui/task.tsx`/`ui/tool.tsx` were /ds-only and left with the kit, ADR-074 stage 4).

## Rendered answers (ADR-053)

The one renderer is kit `Markdown` (react-markdown + the `.markdown` layer in app.css - no
typography plugin, deliberately: it reads as chat, not a document). Everything an agent says
renders through it; user turns stay pre-wrap plain (pinned by test). In conversation surfaces (the
transcript's answers, the bot chat; `Markdown breaks`) a newline inside a paragraph is a line
break (`lib/remark-breaks.ts`, the `remark-breaks` behaviour without the dependency), so a poem
keeps its rows; documents (artifact previews, instructions) stay standard GFM. What the fence law says:

- **Fences dispatch on language.** A registered language (`email` · `message` · `csv` ·
  `mermaid` - one file + one registry row each, `kit/special-blocks.tsx`) renders its block
  card; every other fence renders the lazy `AnswerCodeBlock` (the vendored shiki kit:
  language left, copy right, scroll-not-wrap, no line numbers). The Suspense/static fallback
  is the CSS-styled `<pre>` - degradation stays legible.
- **Fences are presentation only** (HANDOFF §8.6 upheld): a block renders model-authored
  content; its actions are user gestures. Copy is dual-MIME (rich pastes into a mail
  composer); "Use this draft" appears ONLY where a host provides `MarkdownActions.onUseDraft`
  (the task page wires it to the composer's `apiRef.insert`) - absent handler, absent
  button, the capability law. Nothing a fence carries ever executes, fetches, or sends.
- **Live segments split into memoized blocks** (`lib/markdown-stream.ts`): only the tail
  re-parses per delta, its half-typed syntax heals (`healTail`), and a fence open at the
  tail renders its streaming variant (plain code body, block actions hidden). The chunks are
  Fragments under ONE `.markdown` div, so margins and the stream-tail caret behave exactly
  as a single parse. Settled segments render as one parse.
- **Model-authored external images never auto-load** - own-origin/API/relative render,
  anything else is a click-through chip (ADR-046's exfiltration shape, honored client-side).
  One predicate (`lib/image-policy.imageAllowed`) holds it everywhere a model's URL is drawn:
  the gallery cover shows a quiet tile with the chip, and a mermaid source that would fetch
  while it renders shows its source, its output stripped of any foreign fetch. External links open in their own tab; shiki and mermaid live in lazy chunks only.
- **`inert` mode** (kit `Markdown inert`) is for text read on the user's own machine, an
  agent's words about a path it may name: only an `http(s)` link stays a link, any other link
  renders as its words, an image as its alt text, and no special block (email, csv, mermaid)
  mounts - fences fall back to the plain code block. Nothing in the text can open a file or load a page.

## Amendments

Change a rule by changing this file and the kit in the same commit. A screen that needs to
break a rule is a missing kit variant — add the variant, don't fork the recipe inline.
