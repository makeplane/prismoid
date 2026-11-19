/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The live segment's streaming core (ADR-053), pure and hookless. splitBlocks cuts the
// accumulated markdown into stable top-level blocks so the renderer can memoize everything
// but the tail (the whole-doc reparse was O(n^2) over a long answer at ~30fps); healTail
// tidies the tail's half-streamed syntax so mid-token frames never flash raw markers.
//
// The splitter is CONSERVATIVE by law: a wrong split changes meaning (a loose list becomes
// two tight lists), an unsplit run only costs a bigger reparse - when in doubt, don't split.
// Two consequences the renderer leans on: a COLUMN-0 fence opener always STARTS a new block
// and its close ENDS one; an INDENTED opener (a list item's fence) stays inside its
// surrounding block - fence state still guards its interior blank lines - so the tail block
// ends inside at most one open fence, though not necessarily on its first line.
import { FENCE_OPEN, makeCloseMatcher, readFenceOpen } from "./fence-grammar";

// A blank-line split is refused when the next non-blank line could belong to the previous
// block: list markers (a loose list's next item) and indented continuations.
const GLUE_AHEAD = /^( {2,}|\t| {0,3}[-*+][ \t]| {0,3}\d{1,9}[.)][ \t])/;

export function splitBlocks(md: string): string[] {
  const lines = md.split("\n");
  const blocks: string[] = [];
  let cur: string[] = [];
  let fence: { close: RegExp; flushOnClose: boolean } | null = null;

  const flush = () => {
    if (cur.length > 0) {
      blocks.push(cur.join("\n"));
      cur = [];
    }
  };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];

    if (fence) {
      cur.push(line);
      if (fence.close.test(line)) {
        const { flushOnClose } = fence;
        fence = null;
        if (flushOnClose) flush();
      }
      continue;
    }

    const open = readFenceOpen(line);
    if (open) {
      // Only a COLUMN-0 opener forces a block boundary. An indented opener (1-3 spaces)
      // is usually a list item's fence: tearing it out of its list rendered the list as
      // three torn blocks while live. It still enters fence state, so blank lines inside
      // the fence never split - and its close leaves the block open for what follows.
      const atColumnZero = !line.startsWith(" ");
      if (atColumnZero) flush();
      cur.push(line);
      fence = {
        close: makeCloseMatcher(open.char, open.len),
        flushOnClose: atColumnZero,
      };
      continue;
    }

    if (line.trim() === "" && cur.length > 0) {
      // Look past the blank run; split only when what follows starts fresh. A decision is
      // NEVER made against a still-growing line (the last one, unterminated): "- " glues
      // where "-" alone would not, so judging early would re-cut settled blocks - the
      // prefix-stability law this file's test walks character by character.
      let j = i + 1;
      while (j < lines.length && lines[j].trim() === "") j++;
      const lineComplete = j < lines.length - 1 || md.endsWith("\n");
      if (j >= lines.length || !lineComplete) {
        cur.push(line);
        continue; // the tail is still forming - no decision yet
      }
      if (GLUE_AHEAD.test(lines[j]) || FENCE_OPEN.test(lines[j])) {
        cur.push(line); // continuation (a column-0 opener flushes itself; an indented one belongs here)
        continue;
      }
      cur.push(line);
      flush();
      continue;
    }

    cur.push(line);
  }
  flush();
  return blocks.length > 0 ? blocks : [""];
}

/** Line offsets: block i starts at 1-based source line offsets[i] - how a chunk's local
 *  hast positions map back onto the full-source fence scan. */
export function blockLineOffsets(blocks: string[]): number[] {
  const offsets: number[] = [];
  let line = 1;
  for (const b of blocks) {
    offsets.push(line);
    line += b.split("\n").length;
  }
  return offsets;
}

/** Link reference definition lines ('[1]: url'), collected at line starts OUTSIDE fences.
 *  Per-block live parsing severs '[x][1]' from a definition living in another block, so the
 *  renderer appends these to every live chunk's parse input: a definition parses to nothing
 *  visible, and a duplicate (the chunk that already holds it) is ignored - CommonMark keeps
 *  the first. Footnote definitions are deliberately EXCLUDED ('[^1]: note' RENDERS in place
 *  under gfm, so carrying it would duplicate the note); footnote refs resolve at settle. */
export function extractLinkDefs(source: string): string[] {
  const defs: string[] = [];
  let close: RegExp | null = null;
  for (const line of source.split("\n")) {
    if (close) {
      if (close.test(line)) close = null;
      continue;
    }
    const open = readFenceOpen(line);
    if (open) {
      close = makeCloseMatcher(open.char, open.len);
      continue;
    }
    if (/^ {0,3}\[(?!\^)[^\]]+\]:\s+\S/.test(line)) defs.push(line);
  }
  return defs;
}

// Outside fences: heal the inline syntax a mid-token cut leaves dangling. Counting is
// deliberately naive (escaped markers and intra-word underscores are cosmetic edge cases);
// the healed text renders for one frame and the next delta replaces it.
function balanceInline(text: string): string {
  let out = text;
  const ticks = (out.match(/`/g) ?? []).length;
  if (ticks % 2 === 1) out += "`";
  const bold = (out.match(/\*\*/g) ?? []).length;
  if (bold % 2 === 1) out += "**";
  const strike = (out.match(/~~/g) ?? []).length;
  if (strike % 2 === 1) out += "~~";
  return out;
}

// The tail's unterminated last line is dropped when it is a fence marker mid-flight: a
// bare "``"/"~~"-run that might become an opener, or an opener whose language tag could
// still be growing ("```em" on its way to "```email"). It renders on its newline.
function stripableTailLine(last: string): boolean {
  if (/^ {0,3}(`{1,2}|~{1,2})$/.test(last)) return true;
  if (/^ {0,3}`{3,}[^`\n]*$/.test(last)) return true;
  return /^ {0,3}~{3,}[^~\n]*$/.test(last);
}

/** True when the text's last line sits inside an open fence. Since indented openers no
 *  longer start their own block, the tail's fence can begin mid-block ("1. Install:" then
 *  a nested "   ```sh") - so this walks the lines with the shared grammar instead of the
 *  old first-line prefix check. Used by healTail (code is never healed) and by the
 *  renderer's link-definition append (text must never land inside an open fence). */
export function endsInOpenFence(text: string): boolean {
  let close: RegExp | null = null;
  for (const line of text.split("\n")) {
    if (close) {
      if (close.test(line)) close = null;
      continue;
    }
    const open = readFenceOpen(line);
    if (open) close = makeCloseMatcher(open.char, open.len);
  }
  return close !== null;
}

/** Heal the LIVE tail block only. Settled text is never touched (append-only law). */
export function healTail(tail: string): string {
  let lines = tail.split("\n");
  if (lines.length > 0 && stripableTailLine(lines[lines.length - 1])) {
    lines = lines.slice(0, -1);
  }
  const text = lines.join("\n");
  if (text === "") return "";
  // Inside an open fence the text is code - never "heal" code.
  if (endsInOpenFence(text)) return text;
  return balanceInline(text);
}
