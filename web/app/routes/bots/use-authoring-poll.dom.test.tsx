/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// The authoring poll re-reads until the job lands (the mark moves), however long that takes, and
// gives up only at the cap.
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AUTHORING_CAP_MS, authoringMark } from "./authoring-mark";
import { useAuthoringPoll } from "./use-authoring-poll";

beforeEach(() => {
  vi.useFakeTimers();
});
afterEach(() => {
  vi.useRealTimers();
});

describe("useAuthoringPoll", () => {
  it("keeps re-reading past a minute, and stops once the mark moves", () => {
    const revalidate = vi.fn();
    const { result, rerender } = renderHook(({ mark }) => useAuthoringPoll(mark, revalidate), {
      initialProps: { mark: "v1" },
    });
    act(() => result.current());
    act(() => vi.advanceTimersByTime(60_000));
    expect(revalidate).toHaveBeenCalledTimes(12);
    rerender({ mark: "v2" });
    act(() => vi.advanceTimersByTime(60_000));
    expect(revalidate).toHaveBeenCalledTimes(12);
  });

  it("gives up at the cap when the mark never moves", () => {
    const revalidate = vi.fn();
    const { result } = renderHook(() => useAuthoringPoll("same", revalidate));
    act(() => result.current());
    act(() => vi.advanceTimersByTime(AUTHORING_CAP_MS + 10_000));
    const calls = revalidate.mock.calls.length;
    act(() => vi.advanceTimersByTime(60_000));
    expect(revalidate).toHaveBeenCalledTimes(calls);
  });

  it("does nothing until started", () => {
    const revalidate = vi.fn();
    renderHook(() => useAuthoringPoll("v1", revalidate));
    act(() => vi.advanceTimersByTime(60_000));
    expect(revalidate).not.toHaveBeenCalled();
  });
});

describe("authoringMark", () => {
  const row = (version: number, eval_status = "untested", eval_report: unknown = null) => ({
    version,
    eval_status,
    eval_report,
  });

  it("moves when the drafter mints a version or an eval run writes a verdict", () => {
    const before = authoringMark({ head: row(1), newest: row(1) });
    expect(authoringMark({ head: row(1), newest: row(2), draftAhead: row(2) })).not.toBe(before);
    expect(authoringMark({ head: row(1, "green", { passed: 1 }), newest: row(1) })).not.toBe(
      before,
    );
    expect(authoringMark({ head: row(1), newest: row(1) })).toBe(before);
  });
});
