---
name: model
description: >-
  Load when the user wants a spreadsheet or quantitative model - a budget, forecast, cap table, unit economics, or any workbook of formulas that must foot. Not for: prose reports, slides, or reading a spreadsheet someone else made.
capability: fast-executor
allowed-tools: write_file shell publish
produces: spreadsheet
---
You build workbooks whose numbers are right and whose formulas foot.

## The rules of a trustworthy model
- **Formulas, not hardcoded results.** A cell that should be `=B2*B3` and instead reads `41600000` is a lie waiting to drift. The verifier recomputes every formula cell and flags hardcodes.
- **Cross-foot everything.** Rows sum to their total; the total of totals agrees. A model that does not tie out is not a model.
- **Every input in one place**, labelled, so a reader can change an assumption and watch it flow.
- State units in the header, once (`$M`, `%`), and keep the cells bare.

## Building with openpyxl (pinned in the sandbox)
Write ONE Python script to `/work`, run it with the shell (`python3 /work/build.py`), save the
workbook ONCE, then publish. The mechanics that decide whether the file is right:

- **Formulas are strings**: `ws["B10"] = "=SUM(B2:B9)"`. openpyxl stores no computed values -
  the numbers appear when the reader's app opens the file. Every cell in a row labelled
  `Total`/`Subtotal` must hold a full-range `=SUM`, never a typed number, and the range must
  cover the **whole** column of data: `=SUM(B2:B8)` over data that runs to B9 is the classic
  silently-wrong total, and the verifier cross-foots exactly that.
- **Percentages are fractions.** Store `0.15` with `cell.number_format = "0.0%"` → renders
  15.0%. Storing `15` renders 1500.0%.
- **Number formats carry the reading**: money `"#,##0"` (unit in the header), negatives in
  parentheses `"#,##0;(#,##0)"`, multiples `"0.0x"`. Years are text (`"2027"`), or they render
  as `2,027`.
- **Merged cells: write the top-left anchor only.** After `ws.merge_cells("A1:C1")`, every
  other cell in the range is read-only.
- **Cross-sheet references quote spaced names**: `='Assumptions Inputs'!$B$5`. Unquoted, the
  formula breaks.
- **Reading an existing workbook takes two loads**: `load_workbook(path, data_only=True)` for
  cached values (formulas gone), the default load for formulas (values `None`). Never save a
  `data_only` load - it replaces every formula with a literal, permanently.
- Layout is part of correctness: bold the header row (`Font(bold=True)`), size columns
  (`ws.column_dimensions["A"].width = 24`), freeze the header (`ws.freeze_panes = "A2"`), and
  put the answer where a reader looks first.

## What the verifier will do
**A verifier will open your workbook and check the arithmetic. Three things it will not forgive:**

- **A hardcoded total.** A cell in a row labelled "Total" must be a `=SUM(...)` formula, never a typed
  number. A total you typed is a total nothing recomputes - the verifier flags every one.
- **A short-changed range.** A total's `=SUM(B2:B9)` must cover its **whole** column. Stop one row
  short and the total is quietly wrong; the verifier cross-foots and catches it.
- **A broken formula.** No `#REF!`, no divide-by-empty-cell. Every formula must resolve to a number.

So: put the data in cells, put a `=SUM` over the full range for each total, label total rows "Total",
and let the spreadsheet do its own arithmetic. If you want to be sure it is right, `print()` the
numbers from inside your build script *before* saving. Lead the workbook with what it answers: a
clear sheet name, headed columns, and the total where a reader looks first.

## Your bundled tools (beside this file)
- `scripts/check_workbook.py` - the verifier's three spreadsheet checks (hardcoded totals,
  short-changed `=SUM` ranges, broken formulas) as a runnable pre-flight:
  `python3 /skills/model/scripts/check_workbook.py /work/model.xlsx`. Run it after building and
  **before** publishing; fix exactly what it names. It opens the file read-only - it never
  changes your bytes.
- `references/openpyxl.md` - deeper recipes (formats, layout, charts, validation) when the
  ask goes past the basics. Read it when unsure; do not guess an API.

## Gotchas
*Append-only. Every task failure with a fix becomes a line here (§11).*
- Do not paste a computed number where a formula belongs - even if it is correct today.
- Percentages are ratios of like quantities; a margin is `(revenue − cost) / revenue`, shown, not asserted.
- If an input the model needs is not given, leave the cell empty and say so - never invent it to make the sheet complete.
- A total's `=SUM` range that stops one row short of its data is the most common silent error - the range covers the whole block, and the checker script catches the miss before the verifier does.
- Never re-open and re-save the workbook after publishing: an `.xlsx` carries timestamps, so a re-save changes the bytes and harvest fails on the hash mismatch.
