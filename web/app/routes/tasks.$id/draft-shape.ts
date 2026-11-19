/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/** The collection draft's live shape - structure squeezed out of a STREAMING, incomplete
 *  JSON body (`/work/draft.collection.json`, poured verbatim through artifact.delta).
 *
 *  Two laws:
 *  1. Never JSON.parse a partial document - every read is a regex over the text so far,
 *     and every regex requires the CLOSING quote, so a value still being streamed simply
 *     hasn't landed yet (no flicker, no half-words).
 *  2. Structure over order: the stream is split at the first `"rows":` key. Everything
 *     top-level (filename, summary, title, field labels) is read from the HEAD only, and
 *     row/view facts from the TAIL only - so a collection ABOUT titles or labels
 *     (`"cells": {"title": "Dune"}`) can never headline the card, and a stray key in a
 *     row can never mint a phantom column. Quote-escaping already guarantees VALUES
 *     can't fake a key; the split guarantees same-named KEYS can't either.
 *
 *  The organizer streams in schema order - filename → summary → collection{title →
 *  fields → rows → views} - so the breakdown fills top-down as the model writes: the
 *  file gets its real name in the first second, the summary explains it, the columns
 *  appear one by one, the row tail shows what just landed, the views arrive last. */

export type CollectionDraftShape = {
  /** The organizer's own file name (no extension) - the stream's FIRST key, so the card
   *  sheds the scratch-path "draft" name almost immediately. */
  filename: string | null;
  /** The collection's own title, from the head of the stream. */
  title: string | null;
  /** The model's one-line description - streamed before the collection body, so the
   *  card can say WHAT is being built before any row exists. */
  summary: string | null;
  /** Field labels, in schema order, as their objects complete. */
  fields: string[];
  /** Rows so far - one per "cells" key in the tail. */
  rows: number;
  /** The first STRING cell of the last few completed rows, oldest → newest - the
   *  visible tail of the table filling in. First string, not first cell: a rank-first
   *  or date-first table must not lose its heartbeat to a number. */
  recent: string[];
  /** View layouts, once they stream - bounded to the real ViewLayout enum, so a stray
   *  "layout" key in a row can never mint a phantom view. */
  views: string[];
};

/** How much of the row tail the card shows. */
export const RECENT_ROWS = 3;

/** The backend's ViewLayout enum, mirrored as data (schemas/collection.py). */
const LAYOUTS = new Set(["table", "board", "list", "gallery", "gantt", "calendar"]);

// One JSON-string body: any run of non-quote/non-backslash characters or escape pairs.
const STR = String.raw`"((?:[^"\\]|\\.)*)"`;

const FILENAME = new RegExp(String.raw`"filename"\s*:\s*${STR}`);
const SUMMARY = new RegExp(String.raw`"summary"\s*:\s*${STR}`);
const TITLE = new RegExp(String.raw`"title"\s*:\s*${STR}`);
const LABEL = new RegExp(String.raw`"label"\s*:\s*${STR}`, "g");
const CELLS = /"cells"\s*:/g;
// The first STRING value inside a cells object: the lazy prefix skips over non-string
// cells ("rank": 1, dates land as strings and honestly count). `[^{}]` bounds the scan
// to this object; an escaped quote can't open a value (`\"` fails the quote match).
const FIRST_STRING_CELL = new RegExp(String.raw`"cells"\s*:\s*\{[^{}]*?:\s*${STR}`, "g");
const LAYOUT = new RegExp(String.raw`"layout"\s*:\s*${STR}`, "g");
const ROWS_KEY = /"rows"\s*:/;

/** Display-unescape JSON escapes - \uXXXX included (UTF-16 pairs compose naturally). */
function un(s: string): string {
  return s
    .replace(/\\u([0-9a-fA-F]{4})/g, (_, hex: string) => String.fromCharCode(parseInt(hex, 16)))
    .replace(/\\(["\\/nt])/g, (_, c: string) => (c === "n" ? " " : c === "t" ? " " : c));
}

function first(re: RegExp, text: string): string | null {
  const hit = re.exec(text)?.[1];
  return hit != null && hit !== "" ? un(hit) : null;
}

export function collectionDraftShape(text: string): CollectionDraftShape {
  const rowsAt = text.search(ROWS_KEY);
  const head = rowsAt === -1 ? text : text.slice(0, rowsAt);
  const tail = rowsAt === -1 ? "" : text.slice(rowsAt);
  const fields = [...head.matchAll(LABEL)].map((m) => un(m[1]));
  const rows = (tail.match(CELLS) ?? []).length;
  const recent = [...tail.matchAll(FIRST_STRING_CELL)]
    .map((m) => un(m[1]))
    .filter((s) => s !== "")
    .slice(-RECENT_ROWS);
  const views = [...tail.matchAll(LAYOUT)].map((m) => un(m[1])).filter((v) => LAYOUTS.has(v));
  return {
    filename: first(FILENAME, head),
    title: first(TITLE, head),
    summary: first(SUMMARY, head),
    fields,
    rows,
    recent,
    views,
  };
}
