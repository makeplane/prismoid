# openpyxl recipes for workbooks that read well

Read this when the ask goes past headed columns and a `=SUM` - never guess an API.

## Layout

```python
import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

wb = openpyxl.Workbook()
ws = wb.active
ws.title = "Model"                       # never leave "Sheet"
ws.column_dimensions["A"].width = 28     # label column wide, number columns ~12-14
ws.freeze_panes = "B2"                   # headers stay while the reader scrolls
ws.sheet_view.showGridLines = True
```

- Header row: `Font(bold=True)` + a quiet fill (`PatternFill("solid", fgColor="F2F2F2")`).
- Section dividers are labelled rows, not blank space the reader must decode.
- One sheet per concern: `Assumptions`, `Model`, `Summary`. Formulas reference the assumptions
  sheet (`='Assumptions'!$B$2`) so every lever lives in one place.

## Number formats (the reading is the format)

| Content | `number_format` | Note |
|---|---|---|
| Money | `#,##0` | unit in the header (`Revenue ($k)`), cells bare |
| Money, negatives | `#,##0;(#,##0)` | parentheses, never minus-red only |
| Percent | `0.0%` | store the fraction: `0.15` → 15.0% |
| Multiple | `0.0x` | valuations, coverage ratios |
| Year | text `"2027"` | a numeric year renders `2,027` |

## The input convention (finance-standard, reader-expected)

- Hardcoded inputs and levers: **blue** font (`Font(color="0000FF")`).
- Formulas: black (default). Links to another sheet: green (`008000`).
- Cells the reader should fill: light yellow fill + a one-line legend row naming the convention.
- Every assumption is a labelled cell that formulas reference - `=B5*(1+$B$6)`, never `=B5*1.05`.

## Formulas the whole chain can read

- Totals: full-range `=SUM(B2:B9)`. Consistent across every period column - a lone edited cell
  mid-row is the commonest silent error.
- Guard denominators: `=IF(B2=0, "", B3/B2)` - a `#DIV/0!` fails verification.
- Lookups: `INDEX`/`MATCH` over exotic newer functions - every reader app evaluates them.
- openpyxl writes your formula string verbatim; it never validates it. Misspell `=SUMM(...)`
  and the file ships broken. Type carefully, run the bundled checker.

## Charts (native, when asked)

```python
from openpyxl.chart import BarChart, Reference

chart = BarChart()
chart.title = "Revenue by quarter"
data = Reference(ws, min_col=2, min_row=1, max_row=5)      # includes the header for the legend
cats = Reference(ws, min_col=1, min_row=2, max_row=5)
chart.add_data(data, titles_from_data=True)
chart.set_categories(cats)
ws.add_chart(chart, "E2")
```

Native charts recompute with the sheet; a pasted image never does.

## Data validation (dropdowns for filled-in-later cells)

```python
from openpyxl.worksheet.datavalidation import DataValidation

dv = DataValidation(type="list", formula1='"Low,Base,High"', allow_blank=True)
ws.add_data_validation(dv)
dv.add("B2")
```

## Reading an existing workbook

- Two loads, two truths: default load → formula strings, `data_only=True` → last-saved values.
- `data_only=True` on a file no spreadsheet app has saved returns `None` for every formula -
  openpyxl computes nothing.
- Never save the `data_only` load: every formula is replaced by its cached value, permanently.
- `.xlsm`: pass `keep_vba=True` or the macros are gone.
