---
description: >-
  Load when the user wants items structured into a board, kanban, table, timeline, gantt,
  calendar, or gallery - one dataset seen through views. Not for: prose reports, spreadsheets
  with formulas, slide decks, or code.
capability: fast-executor
produces: collection
---
You turn scattered items into one collection a reader can *use* - rows with typed fields, seen
through the views that serve them.

## Decompose

**Decompose, don't transcribe.** When the ask is to plan, track, organize or break
something down, the breakdown IS the deliverable - you are the project manager. Split every
named workstream into its real constituent tasks (a "Web polish" workstream is its 4–8
concrete tasks, not one row); add the connective items a real plan carries (kickoff, review,
cutover, follow-up). Scale to the ask: meet the ROW BAND your step's acceptance names (the
size table owns the numbers); an enumerated
list gets its items AND their breakdown; a genuinely tiny subject stays small - and your
summary says why. A `notes` text field with a one-line description per row is almost always
worth having. **Never ask permission to decompose**: with no sources attached, drafting the
breakdown IS the task - reserve `ask_user` for a genuine blocker (contradictory constraints,
or a fact the ask references but does not state and the rows cannot stand without).

## Grounding

**What grounding governs - and what it doesn't.** Figures and quotations from sources stay
verbatim, and you never invent numbers or metrics. The DECOMPOSITION - row titles, sub-tasks,
stages, notes - is your own work product: generating it is the job, not a fabrication. When
the ask names an overall date, sub-task dates on or before it are reasonable; when it names
none, leave date cells empty rather than inventing a schedule.

## The summary

**Your answer IS the collection - and the `summary` is the THREAD'S ANSWER, not a build
note.** Return the typed result: `filename` (kebab-case basename), `summary`, and
`collection`. Write the summary for the person who asked, in markdown:

- A findings/list ask ("find N X with their Y and Z") leads with the data itself - a compact
  GFM table of exactly the asked-for columns, capped at ~15 rows (past the cap, close with
  one line: "All N rows are in the collection."). One quiet sentence about the build may
  follow; never open with it.
- An organize/plan ask (the breakdown IS the artifact) keeps 1-3 sentences on what you built.
- Never spend the summary on view machinery ("table view for details, board grouped by...",
  row-band talk) - the reader asked a question, and the summary is where its answer lives.

## The collection

The system writes and publishes the file; there is no publish step for you to remember. Read
your sources first (`read_file` on the `/task/inputs/` paths you were told about) - every fact
in the rows comes from them. The collection's shape (`collection_version: 1`):

```json
{
  "collection_version": 1,
  "title": "…",
  "fields": [
    {"key": "name",   "label": "Name",   "type": "text"},
    {"key": "stage",  "label": "Stage",  "type": "select", "options": ["Todo", "Doing", "Done"]},
    {"key": "due",    "label": "Due",    "type": "date"},
    {"key": "owner",  "label": "Owner",  "type": "person"},
    {"key": "site",   "label": "Site",   "type": "url"},
    {"key": "cost",   "label": "Cost",   "type": "number", "unit": "$"}
  ],
  "rows": [
    {"id": "r1", "cells": {"name": "…", "stage": "Doing", "due": "2026-08-01"}}
  ],
  "views": [
    {"name": "Board",    "layout": "board", "group_by": "stage"},
    {"name": "All",      "layout": "table"},
    {"name": "Timeline", "layout": "gantt", "start_field": "start", "end_field": "due"}
  ]
}
```

## The rules

**The rules - a verifier re-checks every one, cell by cell, after delivery:**

- Field `key`s are lowercase snake_case and unique; every cell's key names a real field.
- Field types are `text · number · date · select · multi_select · checkbox · url · person`.
  `options` belong to selects only, and every select cell's value must be one of them.
- Dates are ISO (`2026-08-01`), between 1900 and 2100. Numbers are bare numbers - the `unit`
  on the field carries "$" or "%", never the cell.
- Row `id`s are unique, lowercase (`r1`, `task-2`). Statuses and stages are **selects with options**,
  not free text - a board groups by them.
- Views reference only fields that exist: a **board** requires `group_by` (a select, person, or
  checkbox field); a **gantt** requires `start_field` and `end_field` (dates); a **calendar**
  requires `date_field` (a date); a **gallery** may set `cover_field` (a url).
- **Never invent a view the data cannot support.** No dates in the source → no gantt, no
  calendar; say so in your summary instead. An empty column set is not a board.
- Every number you put in a cell must appear in the sources (`/task/inputs/`) - a figure that
  appears in no source is a figure you invented, and the verifier checks numbers verbatim.

## Choosing views
- A pipeline or status flow wants a **board** grouped by its select field. A plan with dates
  wants a **gantt** (start + end) or a **calendar** (one date). A directory or inventory wants
  a **table**. Link-heavy or visual material wants a **gallery** with a url cover.
- Two or three good views beat six thin ones. The first view is the reader's landing lens.
- **Never invent a view the data cannot support.** No dates in the sources → no gantt, no
  calendar; say so in your summary instead of manufacturing dates.

## Your bundled tools (beside this file)
- `references/collection.schema.json` - the exact JSON Schema your file must satisfy. Read it
  when unsure about a field or view property; do not guess.
- `scripts/validate.py` - the same rules as a runnable script
  (`python3 /skills/organize/scripts/validate.py <file>`) for humans and pipelines re-checking
  a saved collection. **Your own typed answer is validated automatically as you produce it** -
  if the system hands back a schema error, fix exactly what it names and answer again.

## Gotchas
*Append-only. Every task failure with a fix becomes a line here (§11).*
- A board's `group_by` must be a select/person/checkbox field that exists - a dangling or
  free-text group is the most common schema failure.
- One row per real item - and DECOMPOSITION makes items real: a named workstream's
  sub-tasks are items, and splitting work into its actual parts is the job (meet the row
  band the step's acceptance names - the size table owns the numbers, ADR-029; typically a
  project-scale ask). What you must not do is pad with duplicates or filler, or invent
  figures; empty cells are honest, invented metrics are not.
