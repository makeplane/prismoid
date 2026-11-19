# @prismoid/blocks

Prismoid's design system, built on [`@makeplane/propel`](https://www.npmjs.com/package/@makeplane/propel),
Plane's component library (ADR-074). It works like Plane's `@plane/blocks`:

```
@makeplane/propel   primitives + tokens (from npm, pinned in the catalog)
        ↓
@prismoid/blocks        Prismoid's theme + Prismoid-specific composites built from Propel parts
        ↓
web/                pages and features, using Propel primitives and blocks composites
```

## Exports

Every entry is a subpath; there is no barrel at the package root (as in `@plane/blocks`).

| Subpath | Holds |
| ------- | ----- |
| `.` | The stylesheet only (`style` condition): Propel's tokens with Prismoid's monochrome accent, plus `@source` for this package's own classes. `web/app/app.css` imports it as `@import "@prismoid/blocks";` |
| `./sidebar` | The app shell's sidebar: provider, rail, mobile drawer (Propel `Drawer`), menu rows, groups, skeletons |
| `./prompt-input` | The composer's frame, textarea, toolbar, buttons and tooltips (Propel `InputGroup`, `TextArea`, `Button`, `IconButton`, `TooltipPopup`) |
| `./attachment` | Composer attachment chips |
| `./spinner` | The arc spinner the composer uses |
| `./bubble` | Message bubbles and their reactions |
| `./marker` | Thread markers and separators |
| `./message-scroller` | The thread scroller (`@shadcn/react` engine) and its scroll buttons (Propel `IconButton`) |
| `./code-block` | Shiki-highlighted code with the copy button and language select (Propel `IconButton`, `Select`) |
| `./card` | The card surface (Propel has none; same API as the old kit Card) |
| `./toast` | `toast` (sonner's surface the app calls) on Propel's toast manager, and `PrismoidToastProvider` for the root |
| `./cn` | The class merge: tailwind-merge taught Propel's type and border-width scales. web's `cn` re-exports it |

Every composite kept the API it had in web's old shadcn kit (`web/app/components/ui/`, deleted in
stage 4), so moving one was an import-path change at its call sites.

## Rules (Plane's, kept)

- Propel components take no `className` and no `style`: layout goes through `stretch`, a
  wrapper element, or Base UI's `render` prop. Never patch Propel to accept them.
- Propel's variant props are required. Blocks never default them to hide the choice. A
  composite with its own variant names (prompt-input's buttons) maps them onto Propel's in one
  place (`src/internal/kit-button.tsx`).
- Composites are Prismoid's, so they may take `className`. It lands on a wrapper element, or on a
  Propel part through `render`, merged with `src/internal/cn.ts` (tailwind-merge taught
  Propel's type and border-width scales).
- Inside blocks, style with Propel's token classes (`bg-layer-1`, `text-tertiary`,
  `text-body-sm-regular`, ...), never web's semantic names or Tailwind's default type scale.
- Blocks stay presentational: data comes in through props, never from the API client.

Known edge: `attachment` and `message-scroller` use `shimmer` and `scroll-fade-*`, utilities from
`shadcn/tailwind.css`, which web's `app.css` imports (the web app uses `shimmer` too). That sheet
is the one thing kept from the shadcn kit.

## The stages (ADR-074)

1. The look, app-wide: Propel's tokens with the black accent, Prismoid's own tokens re-pointed at
   them. This package's stylesheet.
2. Prismoid's composites move here, rebuilt on Propel.
3. Call sites in `web/` move to Propel primitives slice by slice (buttons and badges, form
   fields, dialogs and menus, the rest), retiring the Tailwind-default compat block in
   `web/app/app.css` as they go.
4. `web/app/components/ui/` is deleted, with the compat block, the kit's `app.css` bridges and the
   dependencies only it used. `/ds` shows Propel and this package instead.
