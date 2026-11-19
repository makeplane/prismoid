/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/** A cheap markdown sniff for the bot-instructions block: instructions that carry real
 *  structure (headings, lists, emphasis, code, links) earn the kit Markdown renderer;
 *  plain prose stays honest pre-wrap text - rendering it as markdown would eat the
 *  author's line breaks. Pure on purpose so it stays testable. */
export function looksLikeMarkdown(text: string): boolean {
  return (
    // A block marker at a line head: heading, list item, ordered item, or quote.
    /^\s{0,3}(#{1,6}\s|[-*+]\s|\d+[.)]\s|>\s)/m.test(text) ||
    // Inline structure: bold, inline code / fences, or a [label](target) link.
    /\*\*[^*\n]+\*\*|`[^`\n]+`|\[[^\]\n]+\]\([^)\n]+\)/.test(text)
  );
}
