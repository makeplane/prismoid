/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The fence extractors' contract (ADR-053): readFence reads what mdast-util-to-hast
// actually emits (verified against its handlers/code.js during the plan's pressure test),
// and scanFences drives ordinals + open-at-tail without ever costing content on a miss.
import { describe, expect, it } from "vitest";
import { readFence, scanFences, type HastElement } from "./fence";

const codeNode = (text: string, className?: string[]): HastElement =>
  ({
    type: "element",
    tagName: "pre",
    properties: {},
    children: [
      {
        type: "element",
        tagName: "code",
        properties: className ? { className } : {},
        children: text === "" ? [] : [{ type: "text", value: text }],
      },
    ],
  }) as unknown as HastElement;

describe("readFence", () => {
  it("reads language and raw source; strips exactly one serializer newline", () => {
    const f = readFence(codeNode("const a = 1\n", ["language-ts"]));
    expect(f).toEqual({ lang: "ts", raw: "const a = 1" });
  });

  it("a bare fence and an indented block carry no language", () => {
    expect(readFence(codeNode("plain\n"))?.lang).toBe("");
  });

  it("an empty fence is empty, not a stray newline", () => {
    expect(readFence(codeNode(""))?.raw).toBe("");
  });

  it("lowercases the tag - authors type ```EMAIL too", () => {
    expect(readFence(codeNode("x\n", ["language-EMAIL"]))?.lang).toBe("email");
  });

  it("refuses non-code shapes instead of guessing", () => {
    const notCode = {
      type: "element",
      tagName: "pre",
      properties: {},
      children: [{ type: "element", tagName: "span", properties: {}, children: [] }],
    } as unknown as HastElement;
    expect(readFence(notCode)).toBeNull();
    expect(readFence(undefined)).toBeNull();
  });
});

describe("scanFences", () => {
  it("numbers openers by 1-based line and marks the tail's open fence", () => {
    const src = "intro\n\n```email\nTo: a\n```\n\n```email\nTo: b";
    const scan = scanFences(src);
    expect(scan).toEqual([
      { line: 3, lang: "email", closed: true },
      { line: 7, lang: "email", closed: false },
    ]);
  });

  it("a fence body's own ``` lines don't open fences; tildes close tildes", () => {
    const src = "~~~md\ninner:\n```js\ncode\n```\n~~~\nafter";
    const scan = scanFences(src);
    expect(scan).toHaveLength(1);
    expect(scan[0]).toMatchObject({ lang: "md", closed: true });
  });

  it("a backtick fence's info string may not contain backticks (CommonMark)", () => {
    expect(scanFences("``` `not-a-fence`\n")).toHaveLength(0);
  });

  it("blockquoted fences are conservatively skipped - the renderer presumes them open while live", () => {
    expect(scanFences("> ```email\n> To: a\n> ```")).toHaveLength(0);
  });
});
