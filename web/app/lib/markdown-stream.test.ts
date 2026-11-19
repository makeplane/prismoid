/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The streaming core's laws (ADR-053): the splitter is conservative (a wrong split changes
// meaning; an unsplit run only costs a reparse), a column-0 fence owns its block while an
// indented one stays in its list's, the settled prefix is stable under append, and the
// healer touches ONLY the tail's own half-typed syntax.
import {
  blockLineOffsets,
  endsInOpenFence,
  extractLinkDefs,
  healTail,
  splitBlocks,
} from "./markdown-stream";
import { describe, expect, it } from "vitest";

describe("splitBlocks", () => {
  it("splits paragraphs; rejoining loses nothing", () => {
    const md = "one\n\ntwo\n\nthree\n";
    const blocks = splitBlocks(md);
    expect(blocks).toHaveLength(3);
    expect(blocks.join("\n")).toBe(md);
  });

  it("defers the split whose lookahead line is still growing - the tail keeps forming", () => {
    expect(splitBlocks("one\n\ntwo\n\nthree")).toHaveLength(2);
  });

  it("never splits a loose list - two tight lists would be a different document", () => {
    expect(splitBlocks("- a\n\n- b\n\n- c")).toHaveLength(1);
  });

  it("never splits an ordered list from its continuation", () => {
    expect(splitBlocks("1. first\n\n2. second")).toHaveLength(1);
    expect(splitBlocks("- item\n\n  continuation paragraph")).toHaveLength(1);
  });

  it("a fence owns its block: blank lines inside stay, the close is a boundary", () => {
    const blocks = splitBlocks("intro\n\n```py\n\na = 1\n\n```\nafter");
    expect(blocks).toEqual(["intro\n", "```py\n\na = 1\n\n```", "after"]);
  });

  it("an unterminated fence runs to the tail as one block", () => {
    const blocks = splitBlocks("intro\n\n```email\nTo: a\n\nbody so far");
    expect(blocks).toHaveLength(2);
    expect(blocks[1]).toBe("```email\nTo: a\n\nbody so far");
  });

  it("an INDENTED opener never tears its list - only column-0 fences force a boundary", () => {
    // The review's exact case: flushing on the 3-space opener cut this into three blocks,
    // so the ordered list rendered torn while live.
    const md = "1. Install:\n   ```bash\n   npm i\n   ```\n2. Run it";
    expect(splitBlocks(md)).toEqual([md]);
  });

  it("blank lines inside an indented fence still never split the surrounding block", () => {
    const md = "- item:\n  ```py\n\n  a = 1\n\n  ```\n- next";
    expect(splitBlocks(md)).toEqual([md]);
  });

  it("the settled prefix is stable as the stream appends", () => {
    const full =
      "Intro paragraph.\n\nSecond thought.\n\n```ts\nconst a = 1\n```\n\n- a\n\n- b\n\nOutro.";
    let prev: string[] = [];
    for (let cut = 1; cut <= full.length; cut++) {
      const now = splitBlocks(full.slice(0, cut));
      expect(now.slice(0, prev.length - 1)).toEqual(prev.slice(0, -1));
      prev = now;
    }
  });

  it("offsets map each block to its 1-based source line", () => {
    const blocks = splitBlocks("a\n\nb\n\nc\n");
    expect(blockLineOffsets(blocks)).toEqual([1, 3, 5]);
  });
});

describe("healTail", () => {
  it("closes half-typed inline syntax so no frame flashes raw markers", () => {
    expect(healTail("this is **bol")).toBe("this is **bol**");
    expect(healTail("call `fn")).toBe("call `fn`");
    expect(healTail("~~gone")).toBe("~~gone~~");
  });

  it("strips a fence marker still growing on the unterminated last line", () => {
    expect(healTail("text\n``")).toBe("text");
    expect(healTail("text\n```em")).toBe("text");
    expect(healTail("``")).toBe("");
  });

  it("keeps an opener whose newline landed, and never heals code", () => {
    expect(healTail("```email\nTo: a")).toBe("```email\nTo: a");
    expect(healTail("```js\nconst tick = `only one")).toBe("```js\nconst tick = `only one");
  });

  it("never heals code even when the open fence began mid-block (an indented list fence)", () => {
    const tail = "1. Install:\n   ```bash\n   echo `hi";
    expect(healTail(tail)).toBe(tail);
  });

  it("balanced text passes through untouched", () => {
    const done = "All **good** and `closed`.";
    expect(healTail(done)).toBe(done);
  });
});

describe("endsInOpenFence", () => {
  it("sees a fence open anywhere in the block, closed fences clear it", () => {
    expect(endsInOpenFence("1. x\n   ```sh\n   ls")).toBe(true);
    expect(endsInOpenFence("```sh\nls\n```")).toBe(false);
    expect(endsInOpenFence("plain prose")).toBe(false);
  });
});

describe("extractLinkDefs", () => {
  it("collects definition lines outside fences only", () => {
    const src =
      "See [x][1].\n\n```md\n[2]: https://in-code.example\n```\n\n[1]: https://a.example/spec";
    expect(extractLinkDefs(src)).toEqual(["[1]: https://a.example/spec"]);
  });

  it("excludes footnote definitions - gfm renders those in place, carrying them would duplicate", () => {
    expect(extractLinkDefs("[^1]: a rendered note\n[ref]: https://a.example")).toEqual([
      "[ref]: https://a.example",
    ]);
  });
});
