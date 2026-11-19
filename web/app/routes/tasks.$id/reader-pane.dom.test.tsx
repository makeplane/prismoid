/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// The reader split pane: the band names the artifact and links to full screen, Escape and the
// close button close it, the download link appears once the artifact reports it, and the
// resize handle commits a clamped share by keyboard, drag and double-click.
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { STORAGE } from "~/lib/storage";
import { ReaderPane, readSplitPct, splitColumns } from "./reader-pane";

vi.mock("~/components/artifact/artifact-view", () => ({
  ArtifactView: (p: {
    artifactId: string;
    readOnly?: boolean;
    onDownloadInfo: (d: { url: string; filename: string | null }) => void;
  }) => (
    <div>
      <p>{`viewing ${p.artifactId}${p.readOnly ? " read-only" : ""}`}</p>
      <button
        type="button"
        onClick={() => p.onDownloadInfo({ url: "https://files.test/a.pdf", filename: "a.pdf" })}
      >
        report download
      </button>
    </div>
  ),
}));

const onClose = vi.fn();
const onPctCommit = vi.fn();

function mount(props: Partial<React.ComponentProps<typeof ReaderPane>> = {}) {
  // the pane sits inside the page's grid row; its parent is what a drag measures
  const row = document.createElement("div");
  document.body.append(row);
  render(
    <MemoryRouter>
      <ReaderPane artifactId="a1" pct={40} onClose={onClose} onPctCommit={onPctCommit} {...props} />
    </MemoryRouter>,
    { container: row },
  );
  return { row, user: userEvent.setup() };
}

const handle = () => screen.getByRole("separator", { name: "Resize artifact pane" });

beforeEach(() => {
  localStorage.clear();
  const captured = new Set<number>();
  Element.prototype.setPointerCapture = (id: number) => void captured.add(id);
  Element.prototype.hasPointerCapture = (id: number) => captured.has(id);
  Element.prototype.releasePointerCapture = (id: number) => void captured.delete(id);
});
afterEach(() => {
  cleanup();
  onClose.mockReset();
  onPctCommit.mockReset();
  localStorage.clear();
  document.body.innerHTML = "";
  document.body.style.cssText = "";
});

describe("split math", () => {
  it("splitColumns clamps the pane share between 15 and 85 percent", () => {
    expect(splitColumns(50)).toBe("minmax(480px, 1fr) minmax(400px, 50%)");
    expect(splitColumns(3)).toContain("15%");
    expect(splitColumns(99)).toContain("85%");
  });

  it("readSplitPct reads the saved share, else a wider default for board kinds", () => {
    expect(readSplitPct("document")).toBe(40);
    expect(readSplitPct("collection")).toBe(52);
    localStorage.setItem(STORAGE.splitPct, "61");
    expect(readSplitPct("document")).toBe(61);
    localStorage.setItem(STORAGE.splitPct, "999");
    expect(readSplitPct()).toBe(85);
  });
});

describe("ReaderPane", () => {
  it("shows the artifact, its title and a full-screen link", () => {
    mount({ title: "Q3 report", readOnly: true });
    expect(screen.getByText("viewing a1 read-only")).toBeTruthy();
    expect(screen.getByText("Q3 report")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Full screen" }).getAttribute("href")).toBe(
      "/artifacts/a1",
    );
  });

  it("has no title in the band when none is given", () => {
    mount();
    expect(screen.queryByText("Q3 report")).toBeNull();
  });

  it("the close button and Escape both close", async () => {
    const { user } = mount();
    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(onClose).toHaveBeenCalledTimes(1);
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it("Escape handled elsewhere does not close", () => {
    mount();
    const e = new KeyboardEvent("keydown", { key: "Escape", cancelable: true });
    e.preventDefault();
    window.dispatchEvent(e);
    expect(onClose).not.toHaveBeenCalled();
  });

  it("offers Download once the artifact reports it, with the file name", async () => {
    const { user } = mount();
    expect(screen.queryByRole("button", { name: "Download" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "report download" }));
    const link = screen.getByRole("button", { name: "Download" });
    expect(link.getAttribute("href")).toBe("https://files.test/a.pdf");
    expect(link.getAttribute("download")).toBe("a.pdf");
  });

  it("the handle exposes the current share", () => {
    mount({ pct: 41.6 });
    expect(handle().getAttribute("aria-valuenow")).toBe("42");
  });

  it("arrow keys widen and narrow the pane in steps of two", () => {
    mount({ pct: 40 });
    fireEvent.keyDown(handle(), { key: "ArrowLeft" });
    expect(onPctCommit).toHaveBeenLastCalledWith(42);
    fireEvent.keyDown(handle(), { key: "ArrowRight" });
    expect(onPctCommit).toHaveBeenLastCalledWith(40);
  });

  it("keyboard resizing never leaves the clamp", () => {
    mount({ pct: 84 });
    fireEvent.keyDown(handle(), { key: "ArrowLeft" });
    fireEvent.keyDown(handle(), { key: "ArrowLeft" });
    expect(onPctCommit).toHaveBeenLastCalledWith(85);
  });

  it("double-click resets to the kind's default", () => {
    mount({ pct: 70, kindHint: "collection" });
    fireEvent.doubleClick(handle());
    expect(onPctCommit).toHaveBeenCalledWith(52);
  });

  it("dragging writes the grid template onto the row live and commits the share on release", () => {
    const { row } = mount({ pct: 40 });
    row.getBoundingClientRect = () => ({ right: 1000, width: 1000 }) as DOMRect;
    fireEvent.pointerDown(handle(), { pointerId: 1, clientX: 600 });
    expect(document.body.style.cursor).toBe("col-resize");
    fireEvent.pointerMove(handle(), { pointerId: 1, clientX: 700 });
    expect(row.style.gridTemplateColumns).toBe(splitColumns(30));
    expect(onPctCommit).not.toHaveBeenCalled();
    fireEvent.pointerUp(handle(), { pointerId: 1, clientX: 700 });
    expect(onPctCommit).toHaveBeenCalledWith(30);
    expect(document.body.style.cursor).toBe("");
  });

  it("a move without a held pointer changes nothing", () => {
    const { row } = mount();
    fireEvent.pointerMove(handle(), { pointerId: 7, clientX: 100 });
    expect(row.style.gridTemplateColumns).toBe("");
    expect(onPctCommit).not.toHaveBeenCalled();
  });
});
