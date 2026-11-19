/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The ONE fence grammar (ADR-053): the opener regex and close matcher that the block
// splitter (lib/markdown-stream.ts) and the fence scan/dispatch (components/kit/fence.ts)
// must agree on line-for-line - they drifted as two hand-copies until this module. It
// lives in lib/ so the pure splitter never imports component code; fence.ts re-exports it
// for the kit side.

/** CommonMark fence opener: a backtick or tilde run of 3+, up to 3 leading spaces, then
 *  the info string's first word (`\S*` - it can never contain the split characters). */
export const FENCE_OPEN = /^ {0,3}(`{3,}|~{3,})[ \t]*(\S*)/;

export interface FenceOpen {
  /** The marker character, "`" or "~". */
  char: string;
  /** Opening run length - a close needs at least this many. */
  len: number;
  /** First word of the info string, lowercased ("" for a bare fence). */
  lang: string;
}

/** Read an opener off one line; null when the line opens nothing - including the
 *  CommonMark rule that a backtick fence's info string may not contain backticks. */
export function readFenceOpen(line: string): FenceOpen | null {
  const m = FENCE_OPEN.exec(line);
  if (!m) return null;
  const marker = m[1];
  if (marker[0] === "`" && m[2].includes("`")) return null;
  return { char: marker[0], len: marker.length, lang: m[2].toLowerCase() };
}

/** The close matcher for one open fence: same character, an equal-or-longer run, nothing
 *  else on the line. Built ONCE at open time - both consumers used to rebuild this RegExp
 *  for every line inside every fence, measured churn on the streaming hot path. */
export function makeCloseMatcher(char: string, len: number): RegExp {
  return new RegExp(`^ {0,3}\\${char}{${len},}[ \\t]*$`);
}
