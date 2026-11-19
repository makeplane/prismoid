/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// The record peek, driven: it titles itself with the row's primary cell, an editor stages cell
// edits, a viewer gets read-only cells, the arrows navigate within bounds, Delete row closes then
// removes, and the resize strip keeps a clamped width that is remembered.
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { parseCollection, type Collection } from "~/lib/collection";
import { STORAGE } from "~/lib/storage";
import { FIXTURE_TEXT } from "../collection.fixtures";
import { RecordPeek } from "./record-peek";

const DATA = (parseCollection(FIXTURE_TEXT) as { ok: true; data: Collection }).data;
const ROW = DATA.rows[0];

const onSetCell = vi.fn();
const onRemoveRow = vi.fn();
const onClose = vi.fn();
const onNav = vi.fn();

function mount(props: Partial<React.ComponentProps<typeof RecordPeek>> = {}) {
  render(
    <RecordPeek
      data={DATA}
      row={ROW}
      canEdit
      onSetCell={onSetCell}
      onRemoveRow={onRemoveRow}
      onClose={onClose}
      onNav={onNav}
      atStart={false}
      atEnd={false}
      index={1}
      count={4}
      {...props}
    />,
  );
  return userEvent.setup();
}

const handle = () => screen.getByRole("separator", { name: "Resize record panel" });
const panel = () => screen.getByRole("complementary", { name: "Record" });

beforeEach(() => {
  localStorage.clear();
  const held = new Set<number>();
  Element.prototype.setPointerCapture = (id: number) => void held.add(id);
  Element.prototype.hasPointerCapture = (id: number) => held.has(id);
  Element.prototype.releasePointerCapture = (id: number) => void held.delete(id);
});
afterEach(() => {
  cleanup();
  for (const m of [onSetCell, onRemoveRow, onClose, onNav]) m.mockReset();
  localStorage.clear();
  document.body.style.cssText = "";
});

describe("RecordPeek content", () => {
  it("is titled by the row's primary cell and says where it sits", () => {
    mount({ index: 2, count: 4 });
    expect(screen.getByRole("heading", { name: "Line expansion" })).toBeTruthy();
    expect(screen.getByText("2 of 4")).toBeTruthy();
  });

  it("an editor edits a text cell, committed on blur", async () => {
    const user = mount();
    const [name] = screen.getAllByRole("textbox") as HTMLInputElement[];
    expect(name.value).toBe("Line expansion");
    await user.clear(name);
    await user.type(name, "Line growth");
    await user.tab();
    expect(onSetCell).toHaveBeenCalledWith("r1", "name", "Line growth");
  });

  it("an emptied text cell stages null; a number commits as a number", async () => {
    const user = mount();
    const [name] = screen.getAllByRole("textbox") as HTMLInputElement[];
    await user.clear(name);
    await user.tab();
    expect(onSetCell).toHaveBeenCalledWith("r1", "name", null);
    const cost = screen.getByRole("spinbutton");
    await user.clear(cost);
    await user.type(cost, "50");
    await user.tab();
    expect(onSetCell).toHaveBeenLastCalledWith("r1", "cost", 50);
  });

  it("select and checkbox cells commit on change", async () => {
    const user = mount();
    await user.selectOptions(screen.getByRole("combobox", { name: "Stage" }), "Done");
    expect(onSetCell).toHaveBeenCalledWith("r1", "stage", "Done");
    await user.click(screen.getByRole("checkbox", { name: "Shipped" }));
    expect(onSetCell).toHaveBeenLastCalledWith("r1", "shipped", true);
  });

  it("a viewer reads the cells and gets no editors or delete", () => {
    mount({ canEdit: false });
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.queryByRole("button", { name: "Delete row" })).toBeNull();
    // the heading plus the read-only cell
    expect(screen.getAllByText("Line expansion")).toHaveLength(2);
    expect(screen.getByText("Dana Whitfield")).toBeTruthy();
  });

  it("Delete row closes the panel, then removes that row", async () => {
    const user = mount();
    await user.click(screen.getByRole("button", { name: "Delete row" }));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onRemoveRow).toHaveBeenCalledWith("r1");
  });
});

describe("RecordPeek navigation", () => {
  it("the arrows step to the previous and next record", async () => {
    const user = mount();
    await user.click(screen.getByRole("button", { name: "Previous record" }));
    expect(onNav).toHaveBeenLastCalledWith(-1);
    await user.click(screen.getByRole("button", { name: "Next record" }));
    expect(onNav).toHaveBeenLastCalledWith(1);
  });

  it("is disabled at either end", () => {
    mount({ atStart: true, atEnd: true });
    expect(
      (screen.getByRole("button", { name: "Previous record" }) as HTMLButtonElement).disabled,
    ).toBe(true);
    expect(
      (screen.getByRole("button", { name: "Next record" }) as HTMLButtonElement).disabled,
    ).toBe(true);
  });

  it("Close record closes", async () => {
    const user = mount();
    await user.click(screen.getByRole("button", { name: "Close record" }));
    expect(onClose).toHaveBeenCalled();
  });
});

describe("RecordPeek width", () => {
  it("starts at the default, or the remembered width clamped", () => {
    mount();
    expect(panel().style.width).toBe("380px");
    cleanup();
    localStorage.setItem(STORAGE.collectionPeek, "9999");
    mount();
    expect(panel().style.width).toBe("560px");
  });

  it("arrow keys step by 32 within 320..560 and remember the width", () => {
    mount();
    fireEvent.keyDown(handle(), { key: "ArrowLeft" });
    expect(panel().style.width).toBe("412px");
    expect(localStorage.getItem(STORAGE.collectionPeek)).toBe("412");
    fireEvent.keyDown(handle(), { key: "ArrowRight" });
    fireEvent.keyDown(handle(), { key: "ArrowRight" });
    fireEvent.keyDown(handle(), { key: "ArrowRight" });
    fireEvent.keyDown(handle(), { key: "ArrowRight" });
    expect(panel().style.width).toBe("320px");
  });

  it("double-click restores the default", () => {
    mount();
    fireEvent.keyDown(handle(), { key: "ArrowLeft" });
    fireEvent.doubleClick(handle());
    expect(panel().style.width).toBe("380px");
    expect(localStorage.getItem(STORAGE.collectionPeek)).toBe("380");
  });

  it("dragging left widens live, and releasing remembers it", () => {
    mount();
    fireEvent.pointerDown(handle(), { pointerId: 1, clientX: 500 });
    expect(document.body.style.cursor).toBe("col-resize");
    fireEvent.pointerMove(handle(), { pointerId: 1, clientX: 450 });
    expect(panel().style.width).toBe("430px");
    expect(localStorage.getItem(STORAGE.collectionPeek)).toBeNull();
    fireEvent.pointerUp(handle(), { pointerId: 1, clientX: 450 });
    expect(localStorage.getItem(STORAGE.collectionPeek)).toBe("430");
    expect(document.body.style.cursor).toBe("");
  });

  it("a drag cannot pass the maximum width", () => {
    mount();
    fireEvent.pointerDown(handle(), { pointerId: 1, clientX: 500 });
    fireEvent.pointerMove(handle(), { pointerId: 1, clientX: 0 });
    expect(panel().style.width).toBe("560px");
  });
});
