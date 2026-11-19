/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// A record's surface in a lens opens by keyboard as it does by click, and stays a plain box
// when nothing opens it.
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PressSurface } from "./press";

afterEach(cleanup);

describe("PressSurface", () => {
  it("is a focusable button that Enter, Space and a click all press", async () => {
    const user = userEvent.setup();
    const onPress = vi.fn();
    render(
      <PressSurface onPress={onPress} selected>
        Q3 plan
      </PressSurface>,
    );
    const surface = screen.getByRole("button", { name: "Q3 plan" });
    expect(surface.getAttribute("aria-current")).toBe("true");
    await user.tab();
    expect(document.activeElement).toBe(surface);
    await user.keyboard("{Enter}");
    await user.keyboard(" ");
    await user.click(surface);
    expect(onPress).toHaveBeenCalledTimes(3);
  });

  it("a key pressed inside a nested control stays that control's", async () => {
    const user = userEvent.setup();
    const onPress = vi.fn();
    render(
      <PressSurface onPress={onPress}>
        <input aria-label="note" />
      </PressSurface>,
    );
    screen.getByRole("textbox", { name: "note" }).focus();
    await user.keyboard("a b{Enter}");
    expect(onPress).not.toHaveBeenCalled();
  });

  it("without onPress it is a plain box: no role, not in the tab order", () => {
    render(<PressSurface>Q3 plan</PressSurface>);
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.getByText("Q3 plan").getAttribute("tabindex")).toBeNull();
  });
});
