/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The pure core of the draft blocks (ADR-053): header parsing shared by ```email and
// ```message fences, and the clipboard payload builders. The fence grammar is deliberately
// human: `Key: value` lines first (one per line, case-insensitive, last repeat wins), a
// blank line, then a markdown body. A fence with no header lines is all body. Tolerant of
// a half-streamed tail - a partial header line parses as far as it goes.

export interface ParsedDraft {
  headers: Record<string, string>;
  body: string;
}

export function parseDraftHeaders(raw: string, keys: readonly string[]): ParsedDraft {
  const lower = new Set(keys.map((k) => k.toLowerCase()));
  const headers: Record<string, string> = {};
  const lines = raw.split("\n");
  let i = 0;
  for (; i < lines.length; i++) {
    const m = /^([A-Za-z][A-Za-z-]*):\s?(.*)$/.exec(lines[i]);
    if (!m || !lower.has(m[1].toLowerCase())) break;
    headers[m[1].toLowerCase()] = m[2].trim();
  }
  // The blank separator belongs to the grammar, not the body.
  if (i < lines.length && lines[i].trim() === "" && Object.keys(headers).length > 0) i++;
  return { headers, body: lines.slice(i).join("\n").replace(/^\n+/, "").trimEnd() };
}

export const EMAIL_KEYS = ["to", "cc", "bcc", "subject"] as const;
export const MESSAGE_KEYS = ["to", "channel", "subject"] as const;

export interface EmailDraft {
  to?: string;
  cc?: string;
  bcc?: string;
  subject?: string;
  body: string;
}

export function parseEmailDraft(raw: string): EmailDraft {
  const { headers, body } = parseDraftHeaders(raw, EMAIL_KEYS);
  return {
    to: headers.to,
    cc: headers.cc,
    bcc: headers.bcc,
    subject: headers.subject,
    body,
  };
}

const ESCAPES: Record<string, string> = {
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
};

export function escapeHtml(text: string): string {
  return text.replace(/[&<>"]/g, (c) => ESCAPES[c]);
}

/** Both clipboard flavors: `text/html` pastes rich into Gmail/Outlook compose; `text/plain`
 *  is the draft verbatim (headers + markdown body). `bodyHtml` is the block's own rendered
 *  body, harvested at click time - this function only assembles. */
export function draftClipboardPayload(
  headers: readonly (readonly [label: string, value: string | undefined])[],
  bodyMarkdown: string,
  bodyHtml: string,
): { html: string; text: string } {
  const present = headers.filter((h): h is [string, string] => Boolean(h[1]));
  const htmlHead = present
    .map(([label, value]) => `<div><b>${escapeHtml(label)}:</b> ${escapeHtml(value)}</div>`)
    .join("");
  const textHead = present.map(([label, value]) => `${label}: ${value}`).join("\n");
  return {
    html: `${htmlHead}${htmlHead ? "<br/>" : ""}<div>${bodyHtml}</div>`,
    text: `${textHead}${textHead ? "\n\n" : ""}${bodyMarkdown}`,
  };
}

/** Copy through the richest door the browser offers; plain text is the floor. Returns the
 *  flavor written so callers can latch feedback. Isolated here so the block components stay
 *  markup + gesture wiring. */
export async function writeClipboard(payload: {
  html: string;
  text: string;
}): Promise<"rich" | "plain"> {
  if (
    typeof ClipboardItem !== "undefined" &&
    typeof navigator !== "undefined" &&
    navigator.clipboard?.write
  ) {
    const item = new ClipboardItem({
      "text/html": new Blob([payload.html], { type: "text/html" }),
      "text/plain": new Blob([payload.text], { type: "text/plain" }),
    });
    await navigator.clipboard.write([item]);
    return "rich";
  }
  // Insecure contexts (and some embedders) expose no clipboard at all - a clear throw
  // instead of dereferencing undefined, so the caller's check latch never fires on a
  // write that went nowhere (@prismoid/blocks/code-block holds the same line).
  if (typeof navigator === "undefined" || !navigator.clipboard?.writeText) {
    throw new Error("Clipboard API not available");
  }
  await navigator.clipboard.writeText(payload.text);
  return "plain";
}
