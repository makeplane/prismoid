/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { describe, expect, it } from "vitest";
import { looksLikeMarkdown } from "./markdownish";

describe("looksLikeMarkdown", () => {
  it("hears block structure", () => {
    expect(looksLikeMarkdown("# Role\nYou are a researcher.")).toBe(true);
    expect(looksLikeMarkdown("Steps:\n- gather\n- verify")).toBe(true);
    expect(looksLikeMarkdown("1. read\n2. summarize")).toBe(true);
    expect(looksLikeMarkdown("> never guess")).toBe(true);
  });
  it("hears inline structure", () => {
    expect(looksLikeMarkdown("Always cite **primary** sources.")).toBe(true);
    expect(looksLikeMarkdown("Call `publish` when done.")).toBe(true);
    expect(looksLikeMarkdown("See [the guide](https://x.y).")).toBe(true);
  });
  it("leaves plain prose alone", () => {
    expect(looksLikeMarkdown("You are a helpful assistant.")).toBe(false);
    expect(looksLikeMarkdown("Answer briefly.\nPrefer tables when asked.")).toBe(false);
    expect(looksLikeMarkdown("Budget is 3 * 4 dollars a day.")).toBe(false);
  });
});
