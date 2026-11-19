/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The operator console's shell - the admin APP's one chrome. Its clientLoader is the guard:
// the session must validate AND carry the admin scope, or the visitor lands back on the
// console's own login (never the product's - this application has no product in it). The nine
// sections are the sidebar: this app is nothing but the console, so the nav says exactly that.

import {
  Link,
  Outlet,
  redirect,
  useLoaderData,
  useLocation,
  useSubmit,
  type ClientLoaderFunctionArgs,
} from "react-router";
import { revalidateShell } from "~/lib/pending-route";
import {
  AlarmClock,
  Bot as BotIcon,
  Building2,
  CircleDollarSign,
  Cpu,
  FolderKanban,
  Gauge,
  ListTodo,
  LogOut,
  ScrollText,
  Users,
} from "lucide-react";
import { Breadcrumbs } from "~/components/layout/breadcrumbs";
import { TOPBAR_SLOT_ID, TOPBAR_START_SLOT_ID } from "~/components/layout/topbar-slot";
import { Badge } from "@makeplane/propel/components/badge";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarHeader,
  SidebarInset,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarProvider,
  SidebarRail,
} from "@prismoid/blocks/sidebar";
import { clearSession } from "~/lib/api/client";
import { requireUser, type User } from "~/lib/auth";
import { useExpiryTimer } from "~/hooks/use-expiry-timer";
import { activeNavPath } from "~/lib/url-state";
import { BrandMark, initials } from "~/components/kit";

export async function clientLoader({ request }: ClientLoaderFunctionArgs) {
  const user = await requireUser(request);
  if (!user.is_admin) {
    // A valid product account without the scope has no business past this door. Drop the
    // session so the login screen greets them cleanly rather than looping the redirect.
    clearSession();
    throw redirect("/login");
  }
  return { user };
}
clientLoader.hydrate = true as const;

// A tab, a filter or an open peek rides the query string: the page already holds its data, so
// the router does not hold that click for another fetch.
export const shouldRevalidate = revalidateShell;

// The console IS these nine sections - the sidebar states the whole application.
const NAV = [
  { to: "/", label: "Overview", icon: Gauge },
  { to: "/users", label: "Users", icon: Users },
  { to: "/workspaces", label: "Workspaces", icon: Building2 },
  { to: "/projects", label: "Projects", icon: FolderKanban },
  { to: "/tasks", label: "Tasks", icon: ListTodo },
  { to: "/bots", label: "Bots", icon: BotIcon },
  { to: "/routines", label: "Routines", icon: AlarmClock },
  { to: "/models", label: "Models", icon: Cpu },
  { to: "/spend", label: "Spend", icon: CircleDollarSign },
  { to: "/audit", label: "Audit", icon: ScrollText },
];

function AdminSidebar({ user }: { user: User }) {
  const { pathname } = useLocation();
  const active = activeNavPath(
    pathname,
    NAV.map((n) => n.to),
  );
  const submit = useSubmit();

  return (
    <Sidebar>
      <SidebarHeader>
        <Link
          to="/"
          className="hover:bg-sidebar-accent focus-visible:ring-sidebar-ring flex min-w-0 items-center gap-2 rounded-md p-1 outline-hidden focus-visible:ring-2"
        >
          <BrandMark />
          <span className="truncate text-body-sm-regular font-semibold">Prismoid</span>
          {/* The mark: one glance says which surface you are on. */}
          <span className="ml-auto flex shrink-0">
            <Badge variant="neutral" size="xs" label="Admin" />
          </span>
        </Link>
      </SidebarHeader>
      <SidebarContent>
        <SidebarGroup>
          <SidebarGroupContent>
            <SidebarMenu>
              {NAV.map(({ to, label, icon: Icon }) => (
                <SidebarMenuItem key={to}>
                  <SidebarMenuButton isActive={active === to} render={<Link to={to} />}>
                    <Icon />
                    <span>{label}</span>
                  </SidebarMenuButton>
                </SidebarMenuItem>
              ))}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
      </SidebarContent>
      <SidebarFooter>
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton
              onClick={() => void submit(null, { method: "post", action: "/logout" })}
              tooltip="Sign out"
            >
              <span className="bg-sidebar-accent text-sidebar-accent-foreground flex size-6 shrink-0 items-center justify-center rounded-full text-[10px] font-medium">
                {initials(user.name)}
              </span>
              <span className="min-w-0 flex-1">
                <span className="block truncate text-body-sm-regular">{user.name}</span>
                <span className="text-muted-foreground block truncate text-caption-md-regular">
                  {user.email}
                </span>
              </span>
              <LogOut className="text-muted-foreground size-4 shrink-0" />
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarFooter>
      <SidebarRail />
    </Sidebar>
  );
}

export default function AdminShell() {
  const { user } = useLoaderData<typeof clientLoader>();
  useExpiryTimer();

  return (
    <SidebarProvider>
      <AdminSidebar user={user} />
      <SidebarInset className="min-w-0">
        <header
          data-shell-header
          className="bg-background/80 sticky top-0 z-10 flex h-14 shrink-0 items-center gap-2 border-b px-gutter backdrop-blur"
        >
          <Breadcrumbs />
          <div id={TOPBAR_START_SLOT_ID} className="flex items-center gap-1 empty:hidden" />
          <div id={TOPBAR_SLOT_ID} className="ml-auto flex items-center gap-3 empty:hidden" />
        </header>
        <Outlet />
      </SidebarInset>
    </SidebarProvider>
  );
}

export { RouteErrorBoundary as ErrorBoundary } from "~/components/layout/route-error";
