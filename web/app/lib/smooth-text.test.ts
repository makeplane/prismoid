/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { describe, expect, it } from "vitest";
import { advanceReveal, revealStep } from "./smooth-text";

describe("revealStep", () => {
  it("releases nothing when caught up", () => {
    expect(revealStep(10, 10)).toBe(0);
    expect(revealStep(10, 4)).toBe(0); // target shrank - the hook snaps, the step stays quiet
  });

  it("drips at least one character", () => {
    expect(revealStep(0, 1)).toBe(1);
    expect(revealStep(99, 100)).toBe(1);
  });

  it("sprints proportionally to the backlog", () => {
    expect(revealStep(0, 1200)).toBe(100);
    expect(revealStep(600, 1200)).toBe(50);
  });

  it("converges: repeated steps always reach the target", () => {
    let shown = 0;
    const target = 5000;
    let guard = 0;
    while (shown < target && guard < 1000) {
      shown += revealStep(shown, target);
      guard += 1;
    }
    expect(shown).toBe(target);
    expect(guard).toBeLessThan(200); // exponential glide, not a crawl
  });
});

describe("advanceReveal", () => {
  it("returns from when there is nothing to do", () => {
    expect(advanceReveal("hello", 5, 3)).toBe(5);
    expect(advanceReveal("hello", 0, 0)).toBe(0);
  });

  it("drips by character on small steps", () => {
    expect(advanceReveal("hello world", 0, 3)).toBe(3);
  });

  it("prefers a word boundary when the step is large", () => {
    const text = "Hello beautiful world and more";
    // Character ideal at 12 is mid-"beautiful"; boundary should not land mid-word.
    const next = advanceReveal(text, 0, 12);
    expect(next).toBeGreaterThan(5);
    // Either ended on whitespace or at a complete word (next char is space/end).
    const atBreak = text[next - 1] === " " || next === text.length || text[next] === " ";
    expect(atBreak).toBe(true);
    expect(text.slice(0, next)).not.toMatch(/beautifu$/); // not mid-"beautiful"
  });

  it("reaches the end exactly", () => {
    expect(advanceReveal("hi", 0, 100)).toBe(2);
  });

  it("converges with revealStep", () => {
    const target = "The **quick** brown fox jumps over the lazy dog.\n\nAgain.";
    let shown = 0;
    let guard = 0;
    while (shown < target.length && guard < 500) {
      const step = revealStep(shown, target.length);
      shown = advanceReveal(target, shown, step);
      guard += 1;
    }
    expect(shown).toBe(target.length);
  });
});
