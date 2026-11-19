/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import {
  AlarmClock,
  ChevronsUpDown,
  FolderKanban,
  Gauge,
  Globe,
  Home,
  BotIcon,
  Inbox,
  LifeBuoy,
  ListTodo,
  LogOut,
  PanelLeft,
  Plug,
  Settings,
  SquarePen,
} from "lucide-react";
import { BrandMark, initials } from "~/components/kit";
import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import { Link, useNavigate, useSubmit } from "react-router";
import { useNavPath } from "~/hooks/use-pending-path";
import { NEW_TASK_HREF } from "~/lib/links";
import { isNewTaskShortcut, NEW_TASK_SHORTCUT } from "~/lib/new-task-shortcut";
import { useAuthConfig } from "~/hooks/use-auth-config";
import { onCloseSettings, onOpenSettings } from "~/lib/settings-bus";
import { STORAGE } from "~/lib/storage";
import { activeNavPath } from "~/lib/url-state";
import { Icon } from "@makeplane/propel/components/icon";
import {
  Menu,
  MenuContent,
  MenuGroup,
  MenuItem,
  MenuSeparator,
  MenuTrigger,
} from "@makeplane/propel/components/menu";
import { IconButton } from "@makeplane/propel/components/icon-button";
import { Tooltip } from "@makeplane/propel/components/tooltip";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuAction,
  SidebarMenuBadge,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarRail,
  SidebarTrigger,
  useSidebar,
} from "@prismoid/blocks/sidebar";
import { SettingsDialog, type SettingsSection } from "~/components/layout/settings-dialog";
import { WorkspaceSwitcher } from "~/components/layout/workspace-switcher";
import { SidebarBots, type SidebarBotRow } from "~/components/layout/sidebar-bots";
import { SidebarRecentTasks } from "~/components/layout/sidebar-recent-tasks";
import { SidebarProjects, type SidebarProjectRow } from "~/components/layout/sidebar-projects";
import type { User } from "~/lib/auth";

// The nav is about work - Home IS the landing (describe, guard, send). Administration
// lives in the settings dialog, opened from the account menu below. Two runs: where the work
// is (and what it wants from you: Inbox sits with it, not at the foot of the list), then what
// the work is set up with.
const WORK = [
  { to: "/", label: "Home", icon: Home },
  { to: "/inbox", label: "Inbox", icon: Inbox },
  { to: "/tasks", label: "Tasks", icon: ListTodo },
  { to: "/projects", label: "Projects", icon: FolderKanban },
  { to: "/routines", label: "Routines", icon: AlarmClock },
];
const SETUP = [
  { to: "/bots", label: "Bots", icon: BotIcon },
  { to: "/connectors", label: "Connectors", icon: Plug }, // the service catalog
  { to: "/allowed-sites", label: "Allowed sites", icon: Globe }, // standing egress grants
];
const NAV = [...WORK, ...SETUP];

/** One run of nav rows. The label rides the expanded sidebar only: the icon rail is one column. */
function NavRun({
  label,
  items,
  active,
  unread,
}: {
  label?: string;
  items: typeof NAV;
  active: string | null | undefined;
  unread: number;
}) {
  return (
    <SidebarGroup>
      {label && <SidebarGroupLabel>{label}</SidebarGroupLabel>}
      <SidebarGroupContent>
        <SidebarMenu>
          {items.map(({ to, label: name, icon: NavIcon }) => (
            <SidebarMenuItem key={to}>
              <SidebarMenuButton isActive={active === to} tooltip={name} render={<Link to={to} />}>
                <NavIcon />
                <span>{name}</span>
              </SidebarMenuButton>
              {/* The pen belongs to Tasks, not to the workspace header: it adds one. */}
              {to === "/tasks" && (
                <Tooltip label="New task" shortcut={NEW_TASK_SHORTCUT} side="right" align="center">
                  <SidebarMenuAction aria-label="New task" render={<Link to={NEW_TASK_HREF} />}>
                    <SquarePen />
                  </SidebarMenuAction>
                </Tooltip>
              )}
              {to === "/inbox" && unread > 0 && (
                <SidebarMenuBadge>{unread > 99 ? "99+" : unread}</SidebarMenuBadge>
              )}
            </SidebarMenuItem>
          ))}
        </SidebarMenu>
      </SidebarGroupContent>
    </SidebarGroup>
  );
}

export function AppSidebar({
  user,
  unread,
  projects,
  bots,
}: {
  user: User;
  unread: number;
  projects: SidebarProjectRow[];
  bots: SidebarBotRow[];
}) {
  const pathname = useNavPath();
  const { toggleSidebar } = useSidebar();
  const active = activeNavPath(
    pathname,
    NAV.map((n) => n.to),
  );
  const submit = useSubmit();
  const supportEmail = useAuthConfig()?.support_email;
  // null = closed; a section id = the dialog open on that section (menu items deep-link).
  const [settingsSection, setSettingsSection] = useState<SettingsSection | null>(null);
  // Other surfaces open the dialog too - via the one settings bus; this owner subscribes.
  // And close it: a panel handing off to a full route (Connectors → /connectors)
  // must put the modal away first, or the destination renders inert behind it.
  useEffect(() => onOpenSettings(setSettingsSection), []);
  useEffect(() => onCloseSettings(() => setSettingsSection(null)), []);
  // ⌘⇧O starts a new task from any page, typing included: the chord is a modifier chord, so
  // it never collides with text entry.
  const navigate = useNavigate();
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!isNewTaskShortcut(e)) return;
      e.preventDefault();
      void navigate(NEW_TASK_HREF);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [navigate]);

  return (
    <Sidebar collapsible="icon">
      {/* Perplexity's header grammar: expanded, the brand sits left and the collapse
          control right - the toggle lives IN the sidebar, not the navbar. Collapsed to the
          icon rail, the logo itself becomes the expand control: it swaps to the panel glyph
          on hover, and the tooltip names the action with its shortcut. */}
      <SidebarHeader>
        {/* Geometry: a size-7 tile behind the link's p-1 centers the logo on the nav-icon
            column below (8px group pad + 10px button inset + half-icon = 26px), and the
            36px row matches the h-9 nav rows - brand and items share one left rhythm. */}
        <div className="flex items-center gap-1 group-data-[collapsible=icon]:hidden">
          <Link
            to="/"
            className="hover:bg-sidebar-accent focus-visible:ring-sidebar-ring flex min-w-0 flex-1 items-center gap-2 rounded-md p-1 outline-hidden focus-visible:ring-2"
          >
            <BrandMark />
            <span className="truncate text-body-sm-regular font-semibold">Prismoid</span>
          </Link>
          <Tooltip label="Collapse sidebar" shortcut="⌘B" side="bottom" align="center">
            <SidebarTrigger />
          </Tooltip>
        </div>
        <div className="hidden flex-col items-center gap-1 group-data-[collapsible=icon]:flex">
          <Tooltip label="Expand sidebar" shortcut="⌘B" side="right" align="center">
            <button
              type="button"
              onClick={toggleSidebar}
              aria-label="Expand sidebar"
              // size-8 hit target (matches the rail's nav squares) around the same
              // size-7 tile the expanded brand wears - no size jump between states.
              className="group/expand focus-visible:ring-sidebar-ring flex size-8 items-center justify-center rounded-md outline-hidden focus-visible:ring-2"
            >
              <BrandMark className="group-hover/expand:hidden group-focus-visible/expand:hidden" />
              {/* The SAME glyph the expanded trigger wears (PanelLeft, as in
                  the blocks sidebar trigger) - collapse and expand read as one control. */}
              <span className="bg-sidebar-accent text-sidebar-accent-foreground hidden size-7 items-center justify-center rounded-md group-hover/expand:flex group-focus-visible/expand:flex">
                <PanelLeft className="size-4" />
              </span>
            </button>
          </Tooltip>
          <NewTaskButton />
        </div>
        <div className="group-data-[collapsible=icon]:hidden">
          <WorkspaceSwitcher workspace={user.workspace} canCreate={user.can_create_workspace} />
        </div>
      </SidebarHeader>

      <SidebarContent>
        <NavRun items={WORK} active={active} unread={unread} />
        <NavRun label="Set up" items={SETUP} active={active} unread={unread} />
        <SidebarProjects projects={projects} />
        <SidebarBots bots={bots} />
        <SidebarRecentTasks />
      </SidebarContent>

      <SidebarFooter>
        <SidebarMenu>
          <SidebarMenuItem>
            <Menu>
              <MenuTrigger
                render={
                  <SidebarMenuButton size="lg">
                    <div className="bg-primary text-primary-foreground flex size-8 shrink-0 items-center justify-center rounded-full text-caption-md-regular font-medium">
                      {initials(user.name)}
                    </div>
                    <div className="grid flex-1 text-left leading-tight">
                      <span className="truncate text-body-sm-regular font-medium">{user.name}</span>
                      <span className="text-muted-foreground truncate text-caption-md-regular">
                        {user.email}
                      </span>
                    </div>
                    <ChevronsUpDown className="ml-auto size-4" />
                  </SidebarMenuButton>
                }
              />
              {/* Overlaying the sidebar itself (side=top, anchor-width), not flying out
                  beside it - and just the actions, nothing else. */}
              <MenuContent side="top" sideOffset={6} sizing="anchor">
                <MenuGroup>
                  <MenuItem
                    label="Settings"
                    icon={<Icon icon={Settings} />}
                    onClick={() => setSettingsSection("general")}
                  />
                  <MenuItem
                    label="Usage"
                    icon={<Icon icon={Gauge} />}
                    onClick={() => setSettingsSection("usage")}
                  />
                  <MenuItem
                    label="Get help"
                    icon={<Icon icon={LifeBuoy} />}
                    onClick={() =>
                      supportEmail
                        ? window.location.assign(`mailto:${supportEmail}`)
                        : setSettingsSection("help")
                    }
                  />
                </MenuGroup>
                <MenuSeparator />
                <MenuItem
                  variant="danger"
                  label="Log out"
                  icon={<Icon icon={LogOut} />}
                  onClick={() => void submit(null, { method: "post", action: "/logout" })}
                />
              </MenuContent>
            </Menu>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarFooter>

      <NavResizer />
      <SettingsDialog
        open={settingsSection !== null}
        initialSection={settingsSection ?? "general"}
        onOpenChange={(o) => !o && setSettingsSection(null)}
      />
    </Sidebar>
  );
}

/** The rail's compose pen, under the logo: the icon rail hides row actions, so the Tasks
 *  row's pen cannot ride there. */
function NewTaskButton() {
  return (
    <Tooltip label="New task" shortcut={NEW_TASK_SHORTCUT} side="right" align="center">
      <IconButton
        nativeButton={false}
        variant="ghost"
        size="sm"
        showTooltip={false}
        aria-label="New task"
        icon={<Icon icon={SquarePen} />}
        render={<Link to={NEW_TASK_HREF} />}
      />
    </Tooltip>
  );
}

// ── the nav's edge, made honest ───────────────────────────────────────────────────────────────

const NAV_MIN = 208;
const NAV_MAX = 400;

/** The sidebar edge control: the rail's cursor always promised `col-resize`
 *  and only ever toggled. Now both are true - **drag resizes**, a plain click (< 4px of
 *  travel) still toggles collapse, arrow keys nudge. The drag writes `--sidebar-width`
 *  on the provider wrapper directly and persists to `STORAGE.navWidth` - CSS-var surgery,
 *  zero re-renders per frame, and every layout that reads the var (the shell inset, the
 *  task split's width clamp) re-solves in the same frame. Collapsed or mobile, it falls
 *  back to the plain toggle rail. */
function NavResizer() {
  const { toggleSidebar, state, isMobile } = useSidebar();
  const dragged = useRef(false);
  const active = useRef(false);

  // Restore the persisted width once - the provider default (16rem) covers first runs.
  useEffect(() => {
    const saved = Number(localStorage.getItem(STORAGE.navWidth));
    if (saved >= NAV_MIN && saved <= NAV_MAX) setNavWidth(saved);
  }, []);

  if (isMobile || state === "collapsed") return <SidebarRail />;

  const beginResize = (e: ReactPointerEvent<HTMLButtonElement>) => {
    e.currentTarget.setPointerCapture(e.pointerId);
    active.current = true;
    navTransitions(false);
    document.body.style.userSelect = "none";
    document.body.style.cursor = "col-resize";
  };
  const resize = (e: ReactPointerEvent<HTMLButtonElement>) => {
    if (!active.current || !e.currentTarget.hasPointerCapture(e.pointerId)) return;
    const w = clampNav(e.clientX);
    if (!dragged.current && Math.abs(w - navWidth()) < 4) return; // still a click
    dragged.current = true;
    setNavWidth(w);
  };
  const endResize = (e: ReactPointerEvent<HTMLButtonElement>) => {
    if (!active.current) return;
    active.current = false;
    e.currentTarget.releasePointerCapture(e.pointerId);
    navTransitions(true);
    document.body.style.userSelect = "";
    document.body.style.cursor = "";
    if (dragged.current) localStorage.setItem(STORAGE.navWidth, String(navWidth()));
    // Reset AFTER the click this pointerup synchronously fires (which must see `true` to
    // suppress the toggle). A pointerCANCEL fires no click - without this reset the flag
    // went stale and silently swallowed the next genuine collapse click.
    requestAnimationFrame(() => {
      dragged.current = false;
    });
  };
  const nudge = (delta: number) => {
    const w = clampNav(navWidth() + delta);
    setNavWidth(w);
    localStorage.setItem(STORAGE.navWidth, String(w));
  };

  return (
    <SidebarRail
      aria-label="Resize or toggle sidebar"
      title="Drag to resize · click to collapse"
      tabIndex={0}
      onPointerDown={beginResize}
      onPointerMove={resize}
      onPointerUp={endResize}
      onPointerCancel={endResize}
      onClick={() => {
        // The click that ends a drag is not a toggle - the flag outlives the gesture
        // just long enough (endResize resets it on the next frame).
        if (dragged.current) return;
        toggleSidebar();
      }}
      onKeyDown={(e) => {
        if (e.key === "ArrowLeft") nudge(-16);
        if (e.key === "ArrowRight") nudge(16);
      }}
    />
  );
}

const clampNav = (x: number) => Math.min(NAV_MAX, Math.max(NAV_MIN, Math.round(x)));

const navWidth = () =>
  document.querySelector<HTMLElement>('[data-slot="sidebar-container"]')?.getBoundingClientRect()
    .width ?? 256;

const setNavWidth = (px: number) =>
  document
    .querySelector<HTMLElement>('[data-slot="sidebar-wrapper"]')
    ?.style.setProperty("--sidebar-width", `${px}px`);

/** The gap and the fixed container both ease width over 200ms - right for a toggle, a
 *  rubber band against the pointer mid-drag. Off while dragging, restored after. */
const navTransitions = (on: boolean) => {
  for (const sel of ['[data-slot="sidebar-gap"]', '[data-slot="sidebar-container"]'])
    document.querySelectorAll<HTMLElement>(sel).forEach((el) => {
      el.style.transitionDuration = on ? "" : "0s";
    });
};
