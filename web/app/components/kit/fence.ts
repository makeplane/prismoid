/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The pure half of fenced-code dispatch (ADR-053): read language and raw source out of the
// hast node react-markdown v10 hands the `pre` override. v10 passes `node` only to function
// components and renders the `code` child BEFORE `pre` sees it, so the raw text is only
// reachable here, on the tree - never through React children.
import type { ExtraProps } from "react-markdown";
import { makeCloseMatcher, readFenceOpen } from "~/lib/fence-grammar";

// The grammar itself lives in lib/ (the splitter shares it and must agree line-for-line);
// re-exported here so kit-side consumers keep one door.
export { FENCE_OPEN, makeCloseMatcher, readFenceOpen } from "~/lib/fence-grammar";

// `@types/hast` is react-markdown's own transitive dep (pnpm strict layout): derive, don't import.
export type HastElement = NonNullable<ExtraProps["node"]>;

export interface FenceInfo {
  /** The fence's language tag, lowercased ("" for an indented block or a bare fence). */
  lang: string;
  /** The source text with exactly one trailing newline stripped (the serializer's, not the author's). */
  raw: string;
}

export interface FenceScanEntry {
  /** 1-based source line of the fence OPENER - the same line the mdast/hast node starts on,
   *  which is how the `pre` override finds its own entry (`node.position.start.line`). */
  line: number;
  lang: string;
  /** False when the fence is still open at end-of-input - the live tail's shape. */
  closed: boolean;
}

/** Scan top-level fence openers (CommonMark backtick + tilde). Deliberately conservative:
 *  fences nested in blockquotes or deep list indents are not tracked - dispatch does NOT
 *  ride this scan (readFence reads the node itself), so an untracked fence still renders
 *  its block; the miss costs its "Draft n of N" label, and while LIVE the renderer presumes
 *  a missing entry OPEN (actions hidden until settle - never actions on half a draft).
 *  Pure; drives ordinal numbering and open-at-tail detection. */
export function scanFences(source: string): FenceScanEntry[] {
  const entries: FenceScanEntry[] = [];
  // The close matcher is built once at open time, not per line (streaming hot path).
  let open: { close: RegExp; entry: FenceScanEntry } | null = null;
  const lines = source.split("\n");
  for (let i = 0; i < lines.length; i++) {
    const text = lines[i];
    if (open) {
      if (open.close.test(text)) {
        open.entry.closed = true;
        open = null;
      }
      continue;
    }
    const m = readFenceOpen(text);
    if (m) {
      const entry: FenceScanEntry = { line: i + 1, lang: m.lang, closed: false };
      entries.push(entry);
      open = { close: makeCloseMatcher(m.char, m.len), entry };
    }
  }
  return entries;
}

/** Read a fenced/indented code block from the `pre` override's node. Null = not a code block
 *  shape (raw HTML can't reach here - react-markdown renders it as literal text). */
export function readFence(node: HastElement | undefined): FenceInfo | null {
  const code = node?.children[0];
  if (!code || code.type !== "element" || code.tagName !== "code") return null;
  if (node.children.length !== 1) return null;

  const classes = Array.isArray(code.properties.className) ? code.properties.className : [];
  const langClass = classes.find(
    (c): c is string => typeof c === "string" && c.startsWith("language-"),
  );
  const lang = langClass ? langClass.slice("language-".length).toLowerCase() : "";

  let raw = "";
  for (const child of code.children) {
    if (child.type === "text") raw += child.value;
  }
  if (raw.endsWith("\n")) raw = raw.slice(0, -1);

  return { lang, raw };
}
