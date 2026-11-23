/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// A verb that takes prose: the confirm waits for a reason as long as its door accepts, and sends
// it trimmed.
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ReasonDialog } from "./confirm";

afterEach(() => {
  cleanup();
  document.body.innerHTML = "";
});

function mount(onConfirm: (reason: string) => Promise<void>, minLength?: number) {
  render(
    <ReasonDialog
      trigger="Pause"
      title="Pause it?"
      description="It stops."
      placeholder="Why?"
      confirmLabel="Pause routine"
      minLength={minLength}
      onConfirm={onConfirm}
    />,
  );
}

describe("ReasonDialog", () => {
  it("waits for a reason of the door's minimum length, then sends it trimmed", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn(async () => {});
    mount(onConfirm, 3);
    await user.click(screen.getByRole("button", { name: "Pause" }));
    const dialog = await screen.findByRole("dialog");
    const confirm = within(dialog).getByRole("button", { name: "Pause routine" });
    expect(within(dialog).getByText("At least 3 characters.")).toBeTruthy();
    await user.type(within(dialog).getByRole("textbox", { name: "Reason" }), " ab ");
    expect(confirm.hasAttribute("disabled")).toBe(true);
    await user.type(within(dialog).getByRole("textbox", { name: "Reason" }), "c");
    expect(confirm.hasAttribute("disabled")).toBe(false);
    await user.click(confirm);
    await waitFor(() => expect(onConfirm).toHaveBeenCalledWith("ab c"));
  });

  it("takes any reason at all by default, with no hint", async () => {
    const user = userEvent.setup();
    mount(vi.fn(async () => {}));
    await user.click(screen.getByRole("button", { name: "Pause" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByRole("textbox", { name: "Reason" }), "x");
    expect(
      within(dialog).getByRole("button", { name: "Pause routine" }).hasAttribute("disabled"),
    ).toBe(false);
    expect(within(dialog).queryByText(/^At least/)).toBeNull();
  });
});
