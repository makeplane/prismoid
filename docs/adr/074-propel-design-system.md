# ADR-074 · Plane's design system: Propel, `@prismoid/blocks`, a black accent

**Status:** accepted · 2026-10-30 · the owner's ask of 2026-10-30 ("replace the current UI
components with makeplane/propel; create a blocks package like [Plane's] for wrapper
components; implement the design system on propel with black as the primary color"). The look
was settled on a mockup the same day: Plane's look, black accent in light, **white accent in
dark** (option A), and a staged migration, Plane's way. **Amends** ADR-017 (the 17px root),
the DESIGN.md radius knob and color ramps, and ADR-071 (the workspace gains `packages/*`).

## Context

The web app's look was its own: the shadcn Mira kit (`web/app/components/ui/`, 42 files) on a
warm-paper and graphite palette, a hand-set `--radius` knob, a 17px root. Plane ships
`@makeplane/propel` (Base UI + Tailwind v4, its tokens shipped as Tailwind source CSS, published
by makeplane under AGPL-3.0-only), and Plane runs it through `@plane/blocks`, the package of
Plane-specific composites built from Propel parts.

Propel's components are strict by design. Content is a prop (`Button label="Save"`), not
children. There is no `className` and no `style` (layout goes through `stretch`, a wrapper, or
Base UI's `render`). Variant axes are required. Plane's migration rewrote every call site to
that API in phases, with codemods. Prismoid has 120 files importing the kit (78 import `Button`).

## Decision

The migration is staged, one stacked PR per stage:

1. **The look, app-wide (this stage).**
   - `packages/blocks` is `@prismoid/blocks`, a workspace package whose root export is the design
     system stylesheet (`style` condition, as `@plane/blocks`). It imports
     `@makeplane/propel/styles` and points Propel's brand ramp at its neutral ramp: the accent
     is black in light (`--brand-default` = `--neutral-1200`) and white in dark
     (`--neutral-white`). Hover and active steps lift off black (dim from white), and primary
     labels need no override because Propel draws them in `text-inverse`.
   - `web/app/app.css` imports `@prismoid/blocks` and points every Prismoid semantic token at Propel's
     (`--background` = `bg-surface-1`, `--sidebar` = `bg-canvas`, `--card`/`--popover` =
     `bg-layer-2`, `--primary` = `bg-accent-primary`, status tones = Propel's status text,
     ...). Propel switches its values per theme, so one block serves light and dark. Only
     Prismoid's own hue ramp and shadows keep a dark block.
   - Themes ride `data-theme` (next-themes `attribute="data-theme"`); Propel owns the `dark`
     variant. The root is Plane's 16px, the body weight Plane's regular (450), and the radius
     scale is Plane's (Tailwind's defaults; the knob is retired).
   - Propel resets Tailwind's color, text, weight, tracking and shadow namespaces to its own.
     Prismoid's call sites still speak Tailwind's names, so app.css gives the defaults back
     verbatim in one marked **compat block**. Stage 3 empties it namespace by namespace. The
     raw palette is gone (five icons in `ui/tool.tsx` moved to status tokens). A build diff of
     every generated selector, before and after, shows nothing else lost.
   - The contrast gate (`lint:contrast`) now resolves `var()` chains through Propel's
     variables, the blocks accent and app.css. Every gated pair passes AA in both schemes.
   - `@base-ui/react` goes 1.6 → 1.9, so Propel (`^1.8.0`) and the kit share one copy. 1.9
     warns when a `Button` renders a non-button, so the 24 link-buttons
     (`render={<a/>}` / `render={<Link/>}`) now pass `nativeButton={false}`.
2. **Prismoid's composites move into `@prismoid/blocks`**, rebuilt on Propel parts: the sidebar, the
   chat message and bubble, the prompt input, tool calls, artifacts, code blocks.
3. **Call sites move to Propel primitives slice by slice** (buttons and badges, form fields,
   dialogs/menus/popovers, the rest). A codemod takes the mechanical cases (a string child
   becomes `label`, `w-full` becomes `stretch="full"`), and people take the rest. Each slice
   retires what it can of the compat block.
4. **`web/app/components/ui/` is deleted.**

Plane's rules hold for `@prismoid/blocks`: no `className`/`style` patches on Propel, required
variants are never defaulted to hide them, and blocks stay presentational.

## Stage 3: where Propel's part did not fit (2026-10-30)

Every call site of the web kit now uses Propel's components, imported directly as Plane's apps
do, or a `@prismoid/blocks` composite. Three places use Base UI's primitive (or a plain element on
Propel's tokens) instead of Propel's ready-made part, each because the part cannot hold a behaviour
the app relies on:

- **Collapsibles** (`kit/disclosure`, the progress tree, the rail sections, the epilogue) use
  `@base-ui/react/collapsible`. Propel's panel always animates height and clips its overflow,
  and DESIGN.md's tree law animates the chevron only; rail rows and focus rings deliberately reach
  past the panel edge.
- **The collection's table lens** stays a `<table>`. Propel's Table refuses `className`/`style`
  (fixed layout, resizable columns, the drag transform), wraps itself in a scroll frame (sticky
  headers against the shell scroller), and pins 44px rows (the gantt rail's row alignment). The
  other tables (verification checks, datasets, previews) are Propel's.
- **The artifact peek** is Propel's Drawer root, backdrop and viewport around Base UI's
  `Drawer.Popup`, because `DrawerPanel` only comes in fixed widths and the peek's width is the
  user's.

Stage 3 also moved two app-wide pieces into `@prismoid/blocks`: `card` (Propel has none; Plane keeps
it in blocks) and `toast`, a toast manager on Propel's toasts that keeps the slice of sonner's
API the app calls. sonner is gone.

Its last slice moved the app's type, weight, tracking and shadow classes onto Propel's scale by
codemod (Tailwind's `text-xs/sm/base/lg/...` → `text-caption-md/body-sm/body-md/h5/...-regular`,
`font-normal` → `font-regular`, shadows → `raised`/`overlay`). The landing hero keeps one
Prismoid-owned size, `text-display` (48px), because Propel's scale stops at h1. web's `cn` is now
blocks' Propel-aware one (`@prismoid/blocks/cn`): stock tailwind-merge read `text-caption-md-regular`
as a colour and dropped the colour class beside it. The compat block stays only for the kit
(`components/ui`) and `/ds`'s specimens of it, and leaves with them in stage 4.

## Stage 4: the kit is gone (2026-10-30)

`web/app/components/ui/` (33 files) is deleted. Its last importer was `/ds`, whose gallery now
shows what the app is made of: a **Propel** section (every primitive the app imports, in
Propel's API) and a **Blocks** section (`@prismoid/blocks`' card, toast, code block, markers,
spinner and composer), with the census and the map rewritten to match. The vendored AI elements
only `/ds` rendered (`task`, `tool`, `artifact`, `loader`, `message`) leave with the kit: the
product's step and tool disclosure is kit `QuietDisclosure` and the task page's progress tree,
and `/ds` shows the spinner where it showed the loader. The `command` specimen (cmdk) is now a
Propel `Combobox` in the project picker's shape. The kit's `Alert`, `HoverCard`, `ScrollArea`
and `Sheet` had no other users and have no successor.

Removed with it, each because only the kit used it:

- the **compat block** in `app.css` (Tailwind's default text, weight, tracking and shadow
  scales): nothing outside the kit spoke those names after stage 3's codemod.
- the kit's **`app.css` bridges**: the overlay shadows on its `data-slot`s and the badge's
  pill radius. Propel's overlays carry Propel's own shadows. `--shadow-modal` went with them;
  `--shadow-overlay` stays for Prismoid's own docks and strips.
- **`cmdk`** (the kit's `command`), **`tw-animate-css`** (the kit's enter/exit classes; Propel
  animates with its own sheet), and web's direct **`class-variance-authority`** and
  **`@shadcn/react`** entries (`@prismoid/blocks` still declares both).
- **`components.json`** (the shadcn CLI's config, pointing at the kit) and the kit's
  `.oxfmtrc.json` ignore entry.

Kept: `shadcn/tailwind.css`, because the app and blocks use its `shimmer` and `scroll-fade-*`
utilities and the gantt rows its `data-*` variants. It is the only reason the `shadcn` package
stays a dev dependency.

## Consequences

- The stylesheet roughly doubles (about 185 to 343 KB raw, 30 to 50 KB gzipped): Propel's
  token sheet (light, dark, high-contrast, extended palettes) and the classes of its whole
  component library, registered as a Tailwind source by Propel itself. No performance budget
  covers CSS (HANDOFF §15). Plane carries the same cost.
- Propel's `text-primary` (body ink) shadows Prismoid's `text-primary` (the accent). With a
  monochrome accent the two are equal in light and within a few lightness points in dark.
  Stage 3 retires Prismoid's name.
- `/ds` shows the radius scale instead of the dial, and DESIGN.md's type, radius and color
  sections describe Propel's system.
- Propel is AGPL-3.0-only. It is makeplane's own code, so Prismoid uses it as Plane does. A
  third party redistributing Prismoid would inherit that license on the web bundle.
- Not in this stage: Propel's icons (Prismoid keeps lucide and hugeicons) and Propel's
  high-contrast themes (Prismoid offers light, dark and system).
