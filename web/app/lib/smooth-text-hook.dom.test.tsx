/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// The paced reveal hook: a settled segment shows in full at once, a live one pours in over
// frames and finishes on the whole text, and a rewrite snaps instead of animating.
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useSmoothText } from "./smooth-text";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

function frames() {
  vi.useFakeTimers();
  let now = 0;
  vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) =>
    setTimeout(() => cb((now += 40)), 40),
  );
  vi.stubGlobal("cancelAnimationFrame", (id: number) => clearTimeout(id));
}

describe("useSmoothText", () => {
  it("shows a settled text in full immediately", () => {
    const { result } = renderHook(() => useSmoothText("all of it", false));
    expect(result.current).toBe("all of it");
  });

  it("pours a live text in over frames, then holds the whole text", () => {
    frames();
    const text = "one two three four five six seven eight nine ten";
    const { result } = renderHook(() => useSmoothText(text, true));
    expect(result.current).toBe("");
    act(() => {
      vi.advanceTimersByTime(80);
    });
    expect(result.current.length).toBeGreaterThan(0);
    expect(result.current.length).toBeLessThan(text.length);
    expect(text.startsWith(result.current)).toBe(true);
    act(() => {
      vi.advanceTimersByTime(5000);
    });
    expect(result.current).toBe(text);
  });

  it("snaps to the full text the moment the segment settles", () => {
    frames();
    const text = "a fairly long streaming sentence that keeps going";
    const { result, rerender } = renderHook(({ live }) => useSmoothText(text, live), {
      initialProps: { live: true },
    });
    expect(result.current).toBe("");
    rerender({ live: false });
    expect(result.current).toBe(text);
  });

  it("a rewrite that does not extend what is shown snaps to the new text", () => {
    frames();
    const { result, rerender } = renderHook(({ t }) => useSmoothText(t, true), {
      initialProps: { t: "hello there general kenobi" },
    });
    act(() => {
      vi.advanceTimersByTime(5000);
    });
    expect(result.current).toBe("hello there general kenobi");
    rerender({ t: "completely different words" });
    expect(result.current).toBe("completely different words");
  });

  it("reduced motion never animates", () => {
    vi.stubGlobal("matchMedia", () => ({ matches: true }));
    const { result } = renderHook(() => useSmoothText("instant", true));
    expect(result.current).toBe("instant");
  });
});
