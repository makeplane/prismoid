/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
import { act, cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PrismoidToastProvider, toast } from "./index";

afterEach(cleanup);

describe("PrismoidToastProvider", () => {
  it("renders its children and shows a toast added through `toast`", async () => {
    render(
      <PrismoidToastProvider>
        <p>page</p>
      </PrismoidToastProvider>,
    );
    expect(screen.getByText("page")).toBeDefined();
    act(() => {
      toast.success("Shared", { description: "Ada can edit it now." });
    });
    expect(await screen.findByText("Shared")).toBeDefined();
    expect(screen.getByText("Ada can edit it now.")).toBeDefined();
  });

  it("a toast's action is a button that runs its handler", async () => {
    const user = userEvent.setup();
    const onUndo = vi.fn();
    render(<PrismoidToastProvider />);
    act(() => {
      toast("Archived", { action: { label: "Undo", onClick: onUndo } });
    });
    await user.click(await screen.findByRole("button", { name: "Undo" }));
    expect(onUndo).toHaveBeenCalledTimes(1);
  });
});
