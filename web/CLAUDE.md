# web - folder rules
Scope: `web/` (the app is `web/app/`). Read with the root CLAUDE.md (it wins on conflict),
docs/engineering.md, and `web/DESIGN.md`, which is the visual and layer contract for every screen.

## What this folder is for
The Prismoid web app: React Router routes (`app/routes/`), the component layers (`app/components/`),
hooks (`app/hooks/`), pure logic and the API client (`app/lib/`, with generated types in
`app/lib/api/*.gen.ts`). It renders what the API and the task stream say; it is NOT where product
rules are decided (that is the backend), and it never invents a type list the server owns.

## Do
- Follow `web/DESIGN.md`'s layers: `@makeplane/propel` and `@prismoid/blocks`
  (`packages/blocks/src/`) are imported by subpath; `components/kit/` is the only place domain
  visuals are defined ("typed twice, it's a kit primitive"); feature folders compose Propel,
  blocks and kit with no new visual vocabulary; `routes/**` is composition only, and a route-local component is never imported across routes.
- Follow its type roles, radius knob, hairlines, colour tokens, motion and states; change a rule
  by changing DESIGN.md and the kit in the same commit.
- Keep the shell thin over a pure, tested fold: the hook wires events, a sibling `.ts` decides,
  and its `*.test.ts` sits beside it (`use-task-stream.ts` over `lib/stream-frame.ts`).
- A feature that adds three or more pieces of state, refs or effects for one concern to an
  existing component lands them as a named hook (`use-<concern>.ts`) with the decisions in a
  sibling pure `.ts` and its tests; the component only composes.
- Model mutually exclusive surfaces or modes as one discriminated-union value, never parallel
  booleans reconciled in each handler. The right edge holds one surface at a time (DESIGN.md,
  "The task page's right side").
- A closed set gets one dispatch site (`gate-dock.tsx`); a new artifact kind is one renderer
  file plus one `registry.ts` line, keyed on the server's `render` hint (ADR-015).
- Promote on the second consumer: a component a second route needs moves to `components/`.
- Streamed text is markdown, append-only, never HTML; binary artifacts stream progress facts,
  never bytes; every delta stream ends in a durable final or snapshot.
- Take API shapes from the generated types (`just web-gen` after `just export-contract`).
- Test what a component does, not only what it shows: an interaction test is a
  `*.dom.test.tsx` that opts into `// @vitest-environment happy-dom`, finds elements by role and
  name, and drives them with user-event, keyboard paths included (ADR-079). Other tests stay in
  node. `pnpm test:coverage` reports coverage.
- A PR that changes a surface DESIGN.md documents amends DESIGN.md in the same PR.

## Never
- Hand-edit a `*.gen.ts`, or put `className` or `style` on a Propel component (ADR-074).
- A raw palette class, hex, `rgb(`, `oklch(` or `bg-white` outside the token layer; a bare or
  arbitrary `rounded`; an em dash in `app/` (`lint:tokens` and `lint:radius` scan all of `app/`
  and `packages/blocks/src`, allowing only `app/app.css`, the token layer).
- Import `~/routes/**` from outside `routes/`, or one route directory from another.
- A React hook in `lib/` in new code; `lib/` stays pure (the existing hook files are
  planned to move).
- A third copy of a primitive: media queries go through one hook, uuids through `lib/uuid.ts`,
  task and project links through `lib/links.ts`, "who am I" through one viewer hook.
- An exported Tailwind class-recipe constant outside `components/kit/`.
- `text-lg`, `text-2xl`, `text-3xl` or `font-bold` outside the type roles DESIGN.md allows.

## Checked by machine
- `just web-check`: `pnpm typecheck`, `pnpm lint` (oxlint, incl. jsx-a11y and the dnd-kit fence
  of ADR-016), `lint:tokens`, `lint:radius`, `lint:dash` (ADR-053), `lint:contrast`, and the vitest suite.
- `quality:web.complexity` (15), `quality:web.depth` (3), `quality:web.function_lines` (300),
  `quality:web.file_lines` (800), `quality:web.metric_disables` (0: a comment switching a
  measured rule off counts), `quality:comments.process_refs`, all covering `packages/blocks/src`
  too. Baselined: 47 functions over complexity 15, the worst being `TaskChat` (127; also 1,357
  lines in the 2,467-line `routes/tasks.$id/route.tsx`) and `lib/task-view.ts`'s `reduceEvent`
  (250); `ScreenPane` (519 lines). Planned refactors pay them down; new code meets the caps.

## Write it like this
- `app/hooks/use-task-stream.ts` with `app/lib/stream-frame.ts`: a thin imperative shell over a
  pure, tested routing fold, and a comment that says so.
- `app/components/tasks/gate-dock.tsx`: one dispatch site for a closed set.
- `app/routes/tasks.$id/steer-stamp.ts`: the rules and the copy pure in a sibling `.ts`; the
  component only reads them.
- `app/routes/home/route.tsx` (104 lines): a loader plus composition of a promoted component.

## Review focus
- State, refs or effects for a new concern added inside `TaskChat`, `ScreenPane` or another
  baselined component instead of a named hook.
- A new case in the `task-view.ts` reducer that belongs to a domain module (a planned split).
- Parallel booleans for exclusive modes, or a second surface able to own the right edge.
- A hand-rolled pattern instance (pill, empty state, list row) that the kit already has.
- A stream consumer that can render HTML, drop the durable final, or leave more than 10 s of
  dead air.
- A documented surface changed without its DESIGN.md amendment: Important.
