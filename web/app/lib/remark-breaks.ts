/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// A newline inside a paragraph is a line break, the way every chat surface reads it: a poem,
// an address or a list of lines keeps its rows instead of running together. This is the
// behaviour of the `remark-breaks` plugin, written out (a dozen lines) rather than added as a
// dependency. Only prose text nodes split; code, inline code and fences carry their own newlines
// as `value`, never as text children, so they are untouched.

type Node = { type: string; value?: string; children?: Node[] };

const NEWLINE = /[ \t]*\r?\n[ \t]*/;

function splitText(value: string): Node[] {
  const out: Node[] = [];
  value.split(NEWLINE).forEach((line, i) => {
    if (i > 0) out.push({ type: "break" });
    if (line) out.push({ type: "text", value: line });
  });
  return out;
}

function walk(node: Node): void {
  if (!node.children) return;
  node.children = node.children.flatMap((child) => {
    if (child.type === "text" && child.value && NEWLINE.test(child.value)) {
      return splitText(child.value);
    }
    walk(child);
    return [child];
  });
}

/** The remark plugin: turns each newline in a paragraph's text into a `break` node. */
export function remarkBreaks() {
  return (tree: Node) => walk(tree);
}
