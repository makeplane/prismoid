/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The iframe cage's CSP, alone in a leaf module (ADR-053 review): preview-view's page case
// and rendition-view's sheet cage inject the same policy, but rendition-view importing it FROM
// preview-view chained the PDF/xlsx rendition chunk onto the whole preview renderer - shiki
// and all - for two small functions. A dependency-free home keeps both chunk graphs honest.

/**
 * The no-network policy: `default-src 'none'` blocks every fetch, inline
 * style/script only, data:-URI media. The page skill builds self-contained pages; this
 * enforces the same law on any bytes that reach the reader by other roads.
 */
export const PAGE_CSP =
  "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; " +
  "img-src data: blob:; font-src data:; media-src data: blob:";

export function withPageCsp(html: string): string {
  // The meta goes at the DOCUMENT FRONT (right after the doctype), not inside <head>: a CSP
  // <meta> only governs what the parser meets AFTER it, and content placed before a literal
  // <head> tag (a <script src>, an <img>) is hoisted into the synthesized head and would fetch
  // before a head-anchored policy existed. Front-of-document is hoisted first, so nothing
  // fetchable can precede the policy. (Residual, accepted: CSP cannot stop a <meta refresh>
  // from navigating the iframe itself - the sandbox still holds the opaque origin either way.)
  const meta = `<meta http-equiv="Content-Security-Policy" content="${PAGE_CSP}">`;
  const doctype = /^\s*<!doctype[^>]*>/i.exec(html);
  if (doctype) {
    const at = doctype.index + doctype[0].length;
    return html.slice(0, at) + meta + html.slice(at);
  }
  return meta + html;
}
