/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// The right edge holds one surface at a time: a live publish opens the reader (unless closed
// by hand, superseded, or the screen is open), the screen and reader displace each other, and
// the rail toggle gives the rail back and remembers the choice.
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { STORAGE } from "~/lib/storage";
import type { ArtifactView } from "~/lib/task-view";
import { useRightSurface } from "./use-right-surface";

const doc = (id: string, over: Partial<ArtifactView> = {}): ArtifactView => ({
  id,
  title: id,
  type: "document",
  ...over,
});

type Props = Parameters<typeof useRightSurface>[0];
const base: Props = {
  machine: { driver: "sandbar" },
  artifacts: [],
  allArtifacts: [],
  live: true,
  superseded: {},
};

function stubViewport(wide: boolean) {
  vi.stubGlobal(
    "matchMedia",
    () =>
      ({
        matches: wide,
        addEventListener: () => undefined,
        removeEventListener: () => undefined,
      }) as unknown as MediaQueryList,
  );
}

function mount(over: Partial<Props> = {}) {
  return renderHook((p: Partial<Props>) => useRightSurface({ ...base, ...over, ...p }), {
    initialProps: {},
  });
}

beforeEach(() => {
  localStorage.clear();
  stubViewport(true);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  localStorage.clear();
});

describe("useRightSurface reader", () => {
  it("a live publish opens the reader and enters artifact mode on a wide screen", () => {
    const a = doc("d1");
    const { result } = mount({ artifacts: [a], allArtifacts: [a] });
    expect(result.current.openArtifact).toBe("d1");
    expect(result.current.openEntry?.id).toBe("d1");
    expect(result.current.openKind).toBe("document");
    expect(result.current.artifactMode).toBe(true);
  });

  it("a settled task's artifacts do not open the reader", () => {
    const a = doc("d1");
    const { result } = mount({ artifacts: [a], allArtifacts: [a], live: false });
    expect(result.current.openArtifact).toBeNull();
    expect(result.current.artifactMode).toBe(false);
  });

  it("a superseded artifact stays closed", () => {
    const a = doc("d1");
    const { result } = mount({ artifacts: [a], superseded: { d1: "d2" } });
    expect(result.current.openArtifact).toBeNull();
  });

  it("closing by hand latches: the same artifact does not reopen, a new revision does", () => {
    const a = doc("d1");
    const { result, rerender } = mount({ artifacts: [a], allArtifacts: [a] });
    act(() => result.current.closeReader());
    expect(result.current.openArtifact).toBeNull();
    rerender({ artifacts: [doc("d1")] });
    expect(result.current.openArtifact).toBeNull();
    rerender({ artifacts: [doc("d2")] });
    expect(result.current.openArtifact).toBe("d2");
  });

  it("opening from the rail opens the reader and closes the narrow rail sheet", () => {
    stubViewport(false);
    const { result } = mount();
    act(() => result.current.setRailSheet(true));
    act(() => result.current.openArtifactFromRail("d5"));
    expect(result.current.openArtifact).toBe("d5");
    expect(result.current.railSheet).toBe(false);
  });

  it("committing a split share persists it and drives the pane share", () => {
    const { result } = mount();
    act(() => result.current.commitSplitPct(42));
    expect(result.current.panePct).toBe(42);
    expect(localStorage.getItem(STORAGE.splitPct)).toBe("42");
  });

  it("writes the grid template on the row only in artifact mode", () => {
    const a = doc("d1");
    const { result } = mount({ artifacts: [a] });
    const row = document.createElement("div");
    (result.current.rowRef as { current: HTMLDivElement | null }).current = row;
    act(() => result.current.setOpenArtifact(null));
    act(() => result.current.setOpenArtifact("d1"));
    expect(row.style.gridTemplateColumns).not.toBe("");
    act(() => result.current.setOpenArtifact(null));
    expect(row.style.gridTemplateColumns).toBe("");
  });
});

describe("useRightSurface screen", () => {
  it("opens only on a task with a Machine, and never a device Machine", () => {
    const { result, rerender } = mount();
    act(() => result.current.openScreenPane());
    expect(result.current.screenOpen).toBe(true);
    rerender({ machine: { driver: "device" } });
    expect(result.current.screenOpen).toBe(false);
    rerender({ machine: null });
    expect(result.current.screenOpen).toBe(false);
  });

  it("the screen displaces the reader, and a reader opened after displaces the screen", () => {
    const { result } = mount();
    act(() => result.current.showArtifact("d1"));
    act(() => result.current.openScreenPane());
    expect(result.current.openArtifact).toBeNull();
    expect(result.current.screenOpen).toBe(true);
    act(() => result.current.showArtifact("d2"));
    expect(result.current.screenOpen).toBe(false);
    expect(result.current.openArtifact).toBe("d2");
  });

  it("a publish does not steal the pane while the screen is open", () => {
    const a = doc("d1");
    const { result, rerender } = mount();
    act(() => result.current.openScreenPane());
    rerender({ artifacts: [a] });
    expect(result.current.openArtifact).toBeNull();
    expect(result.current.screenOpen).toBe(true);
  });

  it("closeScreen puts the screen away", () => {
    const { result } = mount();
    act(() => result.current.openScreenPane());
    act(() => result.current.closeScreen());
    expect(result.current.screenOpen).toBe(false);
  });
});

describe("useRightSurface rail", () => {
  it("starts open unless the saved preference says closed", () => {
    expect(mount().result.current.railPref).toBe(true);
    cleanup();
    localStorage.setItem(STORAGE.taskRail, "closed");
    expect(mount().result.current.railPref).toBe(false);
  });

  it("the toggle on a wide screen flips the rail and saves it", () => {
    const { result } = mount();
    act(() => result.current.toggleRail());
    expect(result.current.railPref).toBe(false);
    expect(localStorage.getItem(STORAGE.taskRail)).toBe("closed");
    act(() => result.current.toggleRail());
    expect(result.current.railPref).toBe(true);
    expect(localStorage.getItem(STORAGE.taskRail)).toBe("open");
  });

  it("the toggle on a narrow screen flips the sheet, not the preference", () => {
    stubViewport(false);
    const { result } = mount();
    act(() => result.current.toggleRail());
    expect(result.current.railSheet).toBe(true);
    expect(result.current.railPref).toBe(true);
    act(() => result.current.toggleRail());
    expect(result.current.railSheet).toBe(false);
  });

  it("the toggle while reading closes the pane and restores the rail", () => {
    localStorage.setItem(STORAGE.taskRail, "closed");
    const a = doc("d1");
    const { result } = mount({ artifacts: [a], allArtifacts: [a] });
    expect(result.current.openArtifact).toBe("d1");
    act(() => result.current.setRailModal(true));
    act(() => result.current.toggleRail());
    expect(result.current.openArtifact).toBeNull();
    expect(result.current.railModal).toBe(false);
    expect(result.current.railPref).toBe(true);
    expect(localStorage.getItem(STORAGE.taskRail)).toBe("open");
  });

  it("the toggle while reading on a narrow screen opens the rail sheet", () => {
    stubViewport(false);
    const { result } = mount();
    act(() => result.current.showArtifact("d1"));
    act(() => result.current.toggleRail());
    expect(result.current.openArtifact).toBeNull();
    expect(result.current.railSheet).toBe(true);
  });
});
