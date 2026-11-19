/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { Outlet } from "react-router";
import { AppSidebar } from "~/components/layout/app-sidebar";
import { Breadcrumbs } from "~/components/layout/breadcrumbs";
import { MobileNavTrigger } from "~/components/layout/mobile-nav-trigger";
import { TOPBAR_SLOT_ID, TOPBAR_START_SLOT_ID } from "~/components/layout/topbar-slot";
import { TaskWindowProvider, toTaskRow, type TaskWindowSeed } from "~/components/tasks/task-window";
import { SidebarInset, SidebarProvider } from "@prismoid/blocks/sidebar";
import { api, unwrap } from "~/lib/api/client";
import { requireUser } from "~/lib/auth";
import { rememberProjectName } from "~/lib/project-names";
import { useExpiryTimer } from "~/hooks/use-expiry-timer";
import { useNavPath, usePendingPage } from "~/hooks/use-pending-path";
import { revalidateShell } from "~/lib/pending-route";
import { PageLoading } from "~/components/kit";
import { cn } from "~/lib/utils";
import type { Route } from "./+types/route";

// The protected app shell. Its clientLoader IS the auth guard: no protected pixel paints until the
// session validates against GET /v1/auth/me (deep links survive via ?next=). It also seeds the inbox
// unread count and the task window (sidebar + Home recents) - each best-effort, since a hiccup on
// either must not blank the app.
export async function clientLoader({ request }: Route.ClientLoaderArgs) {
  const user = await requireUser(request);
  const [unread, projects, bots, taskWindow] = await Promise.all([
    unwrap(api.GET("/v1/inbox")).then(
      (inbox) => inbox.unread,
      () => 0, // leave the badge empty rather than fail the whole shell
    ),
    // the sidebar's Projects window - best-effort like everything the shell seeds
    unwrap(api.GET("/v1/projects", { params: { query: { limit: 8 } } })).then(
      (page) => page.items,
      () => [],
    ),
    // the sidebar's Bots window: PINNED heads only - the pin is the law of the rail
    unwrap(api.GET("/v1/bots", { params: { query: { limit: 200 } } })).then(
      (page) => {
        // pinned is the ONLY law - a draft-only bot is still yours to pin and chat with
        const heads = new Map<string, (typeof page.items)[number]>();
        for (const b of page.items) {
          if (!b.pinned) continue;
          const prev = heads.get(b.name);
          if (!prev || b.version > prev.version) heads.set(b.name, b);
        }
        return [...heads.values()].sort((a, b) => a.name.localeCompare(b.name));
      },
      () => [],
    ),
    Promise.all([
      unwrap(
        api.GET("/v1/tasks", {
          params: { query: { pinned: true, limit: 50 } },
        }),
      ),
      unwrap(
        api.GET("/v1/tasks", {
          params: { query: { pinned: false, limit: 20 } },
        }),
      ),
    ]).then(
      ([pins, recents]): TaskWindowSeed => ({
        pins: pins.items.map(toTaskRow),
        recents: recents.items.map(toTaskRow),
      }),
      () => undefined, // unseeded - the provider fetches on mount instead
    ),
  ]);
  // The sidebar's Projects name themselves for a task page's crumb (no request when one opens).
  for (const s of projects) rememberProjectName(s.id, s.name);
  return { user, unread, projects, bots, taskWindow };
}

// Run the loader on initial hydration (SPA) so a direct load/refresh renders with data,
// not a transient null. Blocks on the root HydrateFallback until it resolves.
clientLoader.hydrate = true as const;

// Nothing here depends on the URL, so a query string that differs between two locations (a tab,
// a filter, leaving a page that held one) is not a reason to re-check the session and re-fetch
// every sidebar window while the click waits.
export const shouldRevalidate = revalidateShell;

export default function Shell({ loaderData }: Route.ComponentProps) {
  const { user, unread, projects, bots, taskWindow } = loaderData;
  useExpiryTimer();
  // Home is the one page with no list under the chrome - its header rule would cut across
  // open canvas. Transparent, not removed: the 1px stays so nothing shifts between routes.
  const onHome = useNavPath() === "/";
  // A change of screen happens on the click: the router holds the old page until the new one's
  // data lands, so the shell puts the page loader in its place meanwhile.
  const pagePending = usePendingPage();

  return (
    <SidebarProvider>
      <TaskWindowProvider initial={taskWindow}>
        <AppSidebar user={user} unread={unread} projects={projects} bots={bots} />
        {/* min-w-0: the inset must shrink beside the sidebar - its default w-full otherwise
            resolves against the viewport and pushes the page 272px past the right edge. */}
        <SidebarInset className="min-w-0">
          {/* The collapse control lives in the SIDEBAR's header (Perplexity's grammar) - the
              navbar opens with the trail. On a phone the sidebar is an off-canvas sheet, so
              the header carries the one control that opens it, before the trail. px-gutter: THE app inset (--spacing-gutter in
              app.css, one knob) - same as the page container, so the first crumb sits
              flush over every page's content edge. */}
          <header
            data-shell-header
            className={cn(
              "bg-background/80 sticky top-0 z-10 flex h-14 shrink-0 items-center gap-2 border-b px-gutter backdrop-blur",
              onHome && "border-transparent",
            )}
          >
            <MobileNavTrigger />
            {!pagePending && <Breadcrumbs />}
            {/* pages project their chrome here (TopbarPortal): "start" sits beside the
                crumb (the inbox's pane controls), "end" is the right cluster (search,
                actions, telemetry). Theme switching lives in Settings → General. */}
            <div id={TOPBAR_START_SLOT_ID} className="flex items-center gap-1 empty:hidden" />
            <div
              id={TOPBAR_SLOT_ID}
              className="ml-auto flex shrink-0 items-center gap-3 empty:hidden"
            />
          </header>
          {pagePending ? <PageLoading /> : <Outlet />}
        </SidebarInset>
      </TaskWindowProvider>
    </SidebarProvider>
  );
}
