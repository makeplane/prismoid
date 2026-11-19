/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// Dataset records as a TABLE (the scatter lane's .jsonl map rendered
// as a wall of raw single-line JSON in the reader). `.jsonl` parses per line into a union-
// of-keys grid; `.csv` rides the same parser the ```csv block uses. Preview-grade and
// honest: a line that will not parse shows raw in its own row, the cap names what it cut,
// and the DownloadCard beneath the shell stays the whole-file door.
import { useMemo } from "react";
import { parseCsv } from "~/components/kit/csv";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@makeplane/propel/components/table";
import type { RendererProps } from "./registry";

const ROW_CAP = 200;
const CELL_CAP = 400;
const STRAY_SHOWN = 20;

interface ParsedDataset {
  columns: string[];
  rows: string[][];
  /** Raw lines that refused to parse as records (jsonl only) - never dropped; the render
   *  length-caps each and names both cuts (chars per line, lines per box). */
  stray: string[];
  total: number;
  /** An unwrapped envelope's scalar facts ("seat: core · exhausted: true") - the one-object
   *  one-array shape (scatter's RecordSet) tables the ARRAY and captions the wrapper. */
  caption?: string;
}

function cell(value: unknown): string {
  if (value === null || value === undefined) return "";
  const s = typeof value === "string" ? value : JSON.stringify(value);
  return s.length > CELL_CAP ? s.slice(0, CELL_CAP) + "…" : s;
}

/** A stray line rides the cell's cap discipline, and the marker names what it cut - one
 *  megabyte one-liner must never reach the DOM as text. */
function strayLine(line: string): string {
  if (line.length <= CELL_CAP) return line;
  return `${line.slice(0, CELL_CAP)}… (+${(line.length - CELL_CAP).toLocaleString("en-US")} chars)`;
}

function isRecord(x: unknown): x is Record<string, unknown> {
  return typeof x === "object" && x !== null && !Array.isArray(x);
}

export function parseJsonl(text: string, cap: number): ParsedDataset {
  // Excel-adjacent tooling fronts UTF-8 with a BOM; JSON.parse refuses it, so left in, the
  // FIRST record always landed in stray and the envelope unwrap below could never fire.
  if (text.charCodeAt(0) === 0xfeff) text = text.slice(1);
  const lines = text.split("\n").filter((l) => l.trim() !== "");
  const columns: string[] = [];
  const objects: Record<string, unknown>[] = [];
  const stray: string[] = [];
  let total = 0;

  // Past the cap a record is a count and nothing more - no key union, no retention. The
  // JSON.parse per line survives the cap on purpose: stray-vs-record cannot be told apart
  // without it, and the honest totals (and the never-dropped stray box) are the contract.
  const keep = (record: Record<string, unknown>) => {
    total++;
    if (objects.length >= cap) return;
    for (const key of Object.keys(record)) {
      if (!columns.includes(key)) columns.push(key);
    }
    objects.push(record);
  };

  for (const line of lines) {
    let value: unknown;
    try {
      value = JSON.parse(line);
    } catch {
      stray.push(line);
      continue;
    }
    if (isRecord(value)) {
      keep(value);
    } else if (Array.isArray(value) && value.length > 0 && value.every(isRecord)) {
      // A line whose top-level value is an ARRAY OF OBJECTS is data, not stray - the
      // whole-file-written-as-one-array shape included. Scalar arrays ([1,2]) stay stray.
      for (const record of value) keep(record);
    } else {
      stray.push(line);
    }
  }
  // The envelope shape (scatter's RecordSet: one object wrapping one array of records):
  // the ARRAY is the dataset - table it, and keep the wrapper's scalars as a caption.
  // Without this the whole harvest sat as one 400-char JSON cell.
  if (total === 1 && objects.length === 1 && stray.length === 0) {
    const only = objects[0];
    const arrayFields = Object.entries(only).filter(
      ([, v]) => Array.isArray(v) && v.length > 0 && v.every(isRecord),
    );
    if (arrayFields.length === 1) {
      const [field, records] = arrayFields[0] as [string, Record<string, unknown>[]];
      const nested: string[] = [];
      for (const r of records.slice(0, cap)) {
        for (const key of Object.keys(r)) {
          if (!nested.includes(key)) nested.push(key);
        }
      }
      const scalars = Object.entries(only)
        .filter(([k]) => k !== field)
        .map(([k, v]) => `${k}: ${cell(v)}`)
        .join(" · ");
      return {
        columns: nested,
        rows: records.slice(0, cap).map((r) => nested.map((c) => cell(r[c]))),
        stray: [],
        total: records.length,
        caption: scalars || undefined,
      };
    }
  }
  return {
    columns,
    rows: objects.map((o) => columns.map((c) => cell(o[c]))),
    stray,
    total,
  };
}

function parseDataset(filename: string, text: string): ParsedDataset {
  if (filename.toLowerCase().endsWith(".csv")) {
    const { rows, more } = parseCsv(text, ROW_CAP + 1);
    const [head, ...body] = rows;
    return {
      // Headers are cells too: a runaway first line must not stretch the table any more
      // than a runaway value would.
      columns: (head ?? []).map((h) => cell(h)),
      rows: body.map((r) => r.map(cell)),
      stray: [],
      total: (head ? body.length : 0) + more,
    };
  }
  return parseJsonl(text, ROW_CAP);
}

export function DatasetView({ artifact, text }: RendererProps) {
  // Parsed once per file content, not once per render: the reader pane re-renders on every
  // SSE frame, and reparsing the whole dataset each frame was a verified live cost.
  const parsed = useMemo(
    () => parseDataset(artifact.filename ?? "", text ?? ""),
    [artifact.filename, text],
  );
  const shown = parsed.rows.length;

  if (parsed.columns.length === 0 && parsed.stray.length === 0) {
    return <p className="text-muted-foreground text-body-sm-regular">The dataset is empty.</p>;
  }

  return (
    <div className="space-y-2">
      {parsed.columns.length > 0 && (
        // The same Propel table SheetCase (preview-view) uses for the identical job - one
        // table voice. Cells override the content's nowrap from inside: a 400-char JSON value
        // must wrap in place, never stretch the row. Header keys are POSITIONS, not names -
        // CSV legally repeats and blanks header names. `isolate`: Propel's sticky z-20 header
        // cells must not paint over the shell's sticky z-10 topbar as the page scrolls.
        <div className="isolate">
          <Table variant="table">
            <TableHeader>
              <TableRow>
                {parsed.columns.map((c, i) => (
                  <TableHead key={i} label={c} />
                ))}
              </TableRow>
            </TableHeader>
            <TableBody>
              {parsed.rows.map((row, r) => (
                <TableRow key={r}>
                  {row.map((value, c) => (
                    <TableCell key={c}>
                      <div className="min-w-24 break-words whitespace-normal">{value}</div>
                    </TableCell>
                  ))}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
      <p className="text-muted-foreground text-caption-md-regular">
        {parsed.total.toLocaleString("en-US")} record{parsed.total === 1 ? "" : "s"}
        {parsed.caption ? ` · ${parsed.caption}` : ""}
        {parsed.total > shown ? ` · first ${shown} shown - download for the whole file` : ""}
      </p>
      {parsed.stray.length > 0 && (
        <div className="ring-foreground/10 overflow-x-auto rounded-lg p-3 ring-1">
          <p className="text-muted-foreground mb-1 text-caption-md-regular">
            {parsed.stray.length} line{parsed.stray.length === 1 ? "" : "s"} did not parse as
            records
            {parsed.stray.length > STRAY_SHOWN
              ? ` - first ${STRAY_SHOWN} of ${parsed.stray.length} shown`
              : ""}
            :
          </p>
          <pre className="font-mono text-caption-md-regular leading-relaxed whitespace-pre-wrap break-words">
            {parsed.stray
              .slice(0, STRAY_SHOWN)
              .map((l) => strayLine(l))
              .join("\n")}
          </pre>
        </div>
      )}
    </div>
  );
}

export default DatasetView;
