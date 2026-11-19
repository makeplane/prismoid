/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// A small honest CSV reader for the ```csv preview block (ADR-053). RFC-4180 quoting
// (double-quote fields, "" escapes, commas and newlines inside quotes), CRLF tolerated.
// Preview-grade by design: it never throws - a malformed tail parses as far as it goes.

export interface CsvPreview {
  rows: string[][];
  /** Rows beyond the render cap (the parse keeps only cap rows; this counts the rest). */
  more: number;
}

export function parseCsv(raw: string, cap: number): CsvPreview {
  // Excel fronts UTF-8 exports with a BOM; left in, it sits inside the first field, so the
  // quote-open check (`field === ""`) misses and a quoted first header splits on its commas.
  if (raw.charCodeAt(0) === 0xfeff) raw = raw.slice(1);
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let quoted = false;
  let parsed = 0;
  let more = 0;

  const push = (endOfRow: boolean) => {
    row.push(field);
    field = "";
    if (endOfRow) {
      const blank = row.length === 1 && row[0].trim() === "";
      if (!blank) {
        if (parsed < cap) rows.push(row);
        else more++;
        parsed++;
      }
      row = [];
    }
  };

  for (let i = 0; i < raw.length; i++) {
    const c = raw[i];
    if (quoted) {
      if (c === '"') {
        if (raw[i + 1] === '"') {
          field += '"';
          i++;
        } else quoted = false;
      } else field += c;
      continue;
    }
    if (c === '"' && field === "") quoted = true;
    else if (c === ",") push(false);
    else if (c === "\n") push(true);
    else if (c !== "\r") field += c;
  }
  if (field !== "" || row.length > 0) push(true);
  return { rows, more };
}

/** The streaming variant holds back a partial trailing line - a row renders only once its
 *  newline lands (the draft-card law: a value appears when it closes). */
export function trimOpenTail(raw: string): string {
  if (raw === "" || raw.endsWith("\n")) return raw;
  const cut = raw.lastIndexOf("\n");
  return cut === -1 ? "" : raw.slice(0, cut + 1);
}
