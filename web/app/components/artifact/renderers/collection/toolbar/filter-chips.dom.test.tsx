/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// The active filter and sort rules as removable pills: nothing shows without rules, each pill
// reads field, operator and value, editing a value or removing a rule stages the right view
// patch, and a sort pill flips direction.
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { parseCollection, type Collection, type CollectionView } from "~/lib/collection";
import { FIXTURE_TEXT } from "../collection.fixtures";
import { FilterChips } from "./filter-chips";

const DATA = (parseCollection(FIXTURE_TEXT) as { ok: true; data: Collection }).data;
const TABLE = DATA.views.find((v) => v.name === "All") as CollectionView;

const onUpdateView = vi.fn();
const withRules = (rules: Partial<Pick<CollectionView, "filter" | "sort">>): CollectionView => ({
  ...TABLE,
  filter: [],
  sort: [],
  ...rules,
});

function mount(rules: Partial<Pick<CollectionView, "filter" | "sort">>) {
  const { container } = render(
    <FilterChips data={DATA} view={withRules(rules)} onUpdateView={onUpdateView} />,
  );
  return { container, user: userEvent.setup() };
}

afterEach(() => {
  cleanup();
  onUpdateView.mockReset();
});

describe("FilterChips", () => {
  it("renders nothing when the view has no rules", () => {
    const { container } = mount({});
    expect(container.innerHTML).toBe("");
  });

  it("a filter pill reads the field label, the operator and its value", () => {
    mount({ filter: [{ field: "name", op: "contains", value: "line" }] });
    expect(screen.getByText("Name")).toBeTruthy();
    expect(screen.getByText("contains")).toBeTruthy();
    expect((screen.getByRole("textbox", { name: "Filter value" }) as HTMLInputElement).value).toBe(
      "line",
    );
  });

  it("an unknown field falls back to its key", () => {
    mount({ filter: [{ field: "ghost", op: "eq", value: "x" }] });
    expect(screen.getByRole("button", { name: "Remove filter on ghost" })).toBeTruthy();
  });

  it("emptiness rules have no value editor", () => {
    mount({ filter: [{ field: "owner", op: "empty", value: null }] });
    expect(screen.getByText("is empty")).toBeTruthy();
    expect(screen.queryByRole("textbox", { name: "Filter value" })).toBeNull();
  });

  it("editing a text value stages the filter with that value on blur", async () => {
    const { user } = mount({
      filter: [
        { field: "name", op: "contains", value: "line" },
        { field: "owner", op: "eq", value: "Mia" },
      ],
    });
    const [first] = screen.getAllByRole("textbox", { name: "Filter value" });
    await user.clear(first);
    await user.type(first, "runbook");
    await user.tab();
    expect(onUpdateView).toHaveBeenCalledWith({
      filter: [
        { field: "name", op: "contains", value: "runbook" },
        { field: "owner", op: "eq", value: "Mia" },
      ],
    });
  });

  it("a select field picks its value from the options", async () => {
    const { user } = mount({ filter: [{ field: "stage", op: "eq", value: "Todo" }] });
    await user.selectOptions(screen.getByRole("combobox", { name: "Filter value" }), "Done");
    expect(onUpdateView).toHaveBeenCalledWith({
      filter: [{ field: "stage", op: "eq", value: "Done" }],
    });
  });

  it("a checkbox field offers Yes and No and stages a boolean", async () => {
    const { user } = mount({ filter: [{ field: "shipped", op: "eq", value: true }] });
    const pick = screen.getByRole("combobox", { name: "Filter value" }) as HTMLSelectElement;
    expect(pick.value).toBe("Yes");
    await user.selectOptions(pick, "No");
    expect(onUpdateView).toHaveBeenCalledWith({
      filter: [{ field: "shipped", op: "eq", value: false }],
    });
  });

  it("a number field stages a number, and an emptied one stages null", async () => {
    const { user } = mount({ filter: [{ field: "cost", op: "gte", value: 10 }] });
    const box = screen.getByRole("spinbutton", { name: "Filter value" });
    await user.clear(box);
    await user.type(box, "25");
    await user.tab();
    expect(onUpdateView).toHaveBeenLastCalledWith({
      filter: [{ field: "cost", op: "gte", value: 25 }],
    });
    await user.clear(box);
    await user.tab();
    expect(onUpdateView).toHaveBeenLastCalledWith({
      filter: [{ field: "cost", op: "gte", value: null }],
    });
  });

  it("a date field stages the picked day", () => {
    mount({ filter: [{ field: "due", op: "lte", value: "2026-08-01" }] });
    const box = screen.getByLabelText("Filter value") as HTMLInputElement;
    expect(box.value).toBe("2026-08-01");
    fireEvent.change(box, { target: { value: "2026-09-30" } });
    expect(onUpdateView).toHaveBeenCalledWith({
      filter: [{ field: "due", op: "lte", value: "2026-09-30" }],
    });
  });

  it("removing a filter drops only that rule", async () => {
    const { user } = mount({
      filter: [
        { field: "name", op: "contains", value: "a" },
        { field: "stage", op: "eq", value: "Done" },
      ],
    });
    await user.click(screen.getByRole("button", { name: "Remove filter on Name" }));
    expect(onUpdateView).toHaveBeenCalledWith({
      filter: [{ field: "stage", op: "eq", value: "Done" }],
    });
  });

  it("a sort pill flips its direction, and removing it drops the sort", async () => {
    const { user } = mount({ sort: [{ field: "due", direction: "asc" }] });
    expect(screen.getByText("↑")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Toggle sort direction for Due" }));
    expect(onUpdateView).toHaveBeenCalledWith({ sort: [{ field: "due", direction: "desc" }] });
    await user.click(screen.getByRole("button", { name: "Remove sort on Due" }));
    expect(onUpdateView).toHaveBeenLastCalledWith({ sort: [] });
  });

  it("a descending sort shows the down arrow and flips back to ascending", async () => {
    const { user } = mount({ sort: [{ field: "cost", direction: "desc" }] });
    expect(screen.getByText("↓")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Toggle sort direction for Cost" }));
    expect(onUpdateView).toHaveBeenCalledWith({ sort: [{ field: "cost", direction: "asc" }] });
  });
});
