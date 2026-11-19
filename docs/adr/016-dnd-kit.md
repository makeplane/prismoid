# ADR-016 — @dnd-kit for collection editing gestures

*Status: accepted · July 2026 · the M3 half of ADR-015 (edits are revisions)*

## Context

ADR-015 M3 makes collections editable: a board card dragged between columns, a row's cells
edited inline — every gesture a **cell edit in disguise** that stages into an immutable
revision (`POST /v1/artifacts/{id}/revise`). Drag-and-drop needs a library: hand-rolled pointer
math that is also keyboard-accessible and screen-reader-announced is a project, not a utility.

The task board's celebrated no-drag stance is untouched: that is about task *status belonging
to the machine* (§8.1). Collection cells are the user's data; drag is legitimate there, and
every drag has a non-drag equivalent (the cell editor), which accessibility requires anyway.

## Decision

**`@dnd-kit/core` + `@dnd-kit/utilities`** (MIT, headless, ~13 kB gz for what we import).
Chosen over: `react-beautiful-dnd` (archived), `pragmatic-drag-and-drop` (heavier surface,
list-centric), hand-rolled pointer math (no keyboard sensor, no announcements, no cancel
semantics). `@dnd-kit/sortable` is deliberately NOT taken — row reordering is a recorded
follow-up gesture, and the dependency arrives when the gesture does.

*(Amended 2026-07-31: the gesture arrived — Collections v4 makes row order a first-class drag
in every lens (table/list gutter grips, gallery tiles, the board's multi-container columns),
so `@dnd-kit/sortable@^10` joins core and utilities under the same fence, the same lazy chunk,
and the same ≤48 kB gz budget (~+4 kB gz). The gantt's bar move/resize stays hand-rolled
pointer math on purpose: a continuous day-snapped axis is not a droppable-target problem, and
its non-drag path remains the record peek's date editors.)*

**Containment (the anti-bloat rules, enforced):**

- dnd-kit is imported ONLY under `web/app/components/artifact/renderers/collection/**` — an
  eslint `no-restricted-imports` fence fails any import elsewhere. *(Amended 2026-07-31: the
  fence was `collection/editor/**` when drag lived behind an Edit button; the collections-v2
  quality pass made drag the board's own grammar — Trello's, direct manipulation, no mode — so
  the dep's home is the collection chunk itself. The separate editor chunk was absorbed.)*
- The collection renderer is a **lazy chunk**: the task page's critical path still pays 0 bytes.
  Read-only viewers get the same board minus the handles — dnd-kit's hooks are mounted only when
  the viewer can save.
- Budgets (ride the perf drill cadence, like every §15 number): the collection chunk including
  dnd-kit ≤ 48 kB gz; task-page critical path +0.

## Consequences

- One HANDOFF §3 row (this ADR is its "why this, not the alternative").
- Headless means every pixel stays on the kit's tokens — no widget-library design fork.
- The keyboard sensor + announcements come from the library; the non-drag path (cell editor)
  remains the a11y floor either way.
