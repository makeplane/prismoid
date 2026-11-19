/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
import { SidebarProvider, useSidebar } from "@prismoid/blocks/sidebar";
import { act, cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { MobileNavTrigger } from "./mobile-nav-trigger";

function OpenProbe() {
  const { openMobile } = useSidebar();
  return <output aria-label="nav state">{openMobile ? "open" : "closed"}</output>;
}

function renderAt(width: number) {
  window.innerWidth = width;
  return render(
    <SidebarProvider>
      <MobileNavTrigger />
      <OpenProbe />
    </SidebarProvider>,
  );
}

afterEach(() => {
  cleanup();
  window.innerWidth = 1024;
});

describe("MobileNavTrigger", () => {
  it("on a phone, the header gets a button that opens the nav", async () => {
    // Regression: the only trigger lived inside the off-canvas sidebar, so a phone had no
    // way to reach Tasks, Inbox or Settings.
    const user = userEvent.setup();
    renderAt(390);
    const button = await screen.findByRole("button", { name: "Toggle Sidebar" });
    expect(screen.getByLabelText("nav state").textContent).toBe("closed");
    await user.click(button);
    expect(screen.getByLabelText("nav state").textContent).toBe("open");
  });

  it("on a wide screen it renders nothing - the sidebar keeps its own control", async () => {
    renderAt(1280);
    await act(async () => {});
    expect(screen.queryByRole("button", { name: "Toggle Sidebar" })).toBeNull();
  });
});
