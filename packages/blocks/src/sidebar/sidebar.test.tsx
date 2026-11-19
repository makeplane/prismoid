/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarInset,
  SidebarMenu,
  SidebarMenuBadge,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarProvider,
  SidebarRail,
  SidebarTrigger,
  useSidebar,
} from "./index";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function State() {
  const { state } = useSidebar();
  return <output aria-label="sidebar state">{state}</output>;
}

function Shell({
  defaultOpen,
  open,
  onOpenChange,
}: {
  defaultOpen?: boolean;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
}) {
  return (
    <SidebarProvider defaultOpen={defaultOpen} open={open} onOpenChange={onOpenChange}>
      <Sidebar collapsible="icon">
        <SidebarHeader>Prismoid</SidebarHeader>
        <SidebarContent>
          <SidebarGroup>
            <SidebarGroupLabel>Work</SidebarGroupLabel>
            <SidebarMenu>
              <SidebarMenuItem>
                <SidebarMenuButton isActive>Inbox</SidebarMenuButton>
                <SidebarMenuBadge>3</SidebarMenuBadge>
              </SidebarMenuItem>
              <SidebarMenuItem>
                <SidebarMenuButton>Tasks</SidebarMenuButton>
              </SidebarMenuItem>
            </SidebarMenu>
          </SidebarGroup>
        </SidebarContent>
        <SidebarFooter>Footer</SidebarFooter>
        <SidebarRail />
      </Sidebar>
      <SidebarInset>
        <SidebarTrigger />
        <State />
      </SidebarInset>
    </SidebarProvider>
  );
}

const state = () => screen.getByRole("status", { name: "sidebar state" }).textContent;

describe("Sidebar", () => {
  it("opens expanded, and the trigger collapses and expands it", async () => {
    const user = userEvent.setup();
    render(<Shell />);
    expect(state()).toBe("expanded");
    const [trigger] = screen.getAllByRole("button", { name: "Toggle Sidebar" });
    await user.click(trigger!);
    expect(state()).toBe("collapsed");
    await user.click(trigger!);
    expect(state()).toBe("expanded");
  });

  it("Cmd+B or Ctrl+B toggles it from anywhere", async () => {
    const user = userEvent.setup();
    render(<Shell defaultOpen={false} />);
    expect(state()).toBe("collapsed");
    await user.keyboard("{Meta>}b{/Meta}");
    expect(state()).toBe("expanded");
    await user.keyboard("{Control>}b{/Control}");
    expect(state()).toBe("collapsed");
  });

  it("controlled, it asks its owner instead of changing itself", async () => {
    const user = userEvent.setup();
    const onOpenChange = vi.fn();
    render(<Shell open onOpenChange={onOpenChange} />);
    await user.click(screen.getAllByRole("button", { name: "Toggle Sidebar" })[0]!);
    expect(onOpenChange).toHaveBeenCalledWith(false);
    expect(state()).toBe("expanded");
  });

  it("the rail is a second, named toggle", async () => {
    const user = userEvent.setup();
    render(<Shell />);
    const toggles = screen.getAllByRole("button", { name: "Toggle Sidebar" });
    expect(toggles).toHaveLength(2);
    await user.click(toggles[1]!);
    expect(state()).toBe("collapsed");
  });

  it("menu buttons are buttons, the active one says so, and the main area is a landmark", () => {
    render(<Shell />);
    const inbox = screen.getByRole("button", { name: "Inbox" });
    expect(inbox.getAttribute("data-active")).not.toBeNull();
    expect(screen.getByRole("button", { name: "Tasks" }).getAttribute("data-active")).toBeNull();
    expect(screen.getByRole("main")).toBeDefined();
    expect(screen.getAllByRole("list").length).toBeGreaterThan(0);
  });

  it("useSidebar outside a provider fails loudly", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    expect(() => render(<State />)).toThrow();
  });
});
