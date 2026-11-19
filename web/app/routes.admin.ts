/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { type RouteConfig, index, layout, route } from "@react-router/dev/routes";

// ─────────────────────────────────────────────────────────────────────────────
// THE OPERATOR CONSOLE'S APP MAP - a SEPARATE application (APP_TARGET=admin),
// built to its own bundle and served on its own domain (admin.$DOMAIN), so the
// public app never ships a byte of it and the ingress can fence it to the
// tailnet. Same codebase, same kit, same typed client; different front door:
// its login refuses non-admin accounts outright, and the shell re-proves the
// scope on every load. The nine sections are the sidebar - this app is nothing
// but the console.
// ─────────────────────────────────────────────────────────────────────────────
export default [
  route("login", "routes/admin-login/route.tsx"),
  route("logout", "routes/logout/route.tsx"),

  layout("routes/admin-shell/route.tsx", [
    layout("routes/shell/container.tsx", [
      index("routes/admin/overview/route.tsx"), //  /        health · alerts · spend at a glance
      route("users", "routes/admin/users/route.tsx"),
      route("users/:id", "routes/admin/users.$id/route.tsx"),
      route("workspaces", "routes/admin/workspaces/route.tsx"), //         every workspace: its standing
      route("workspaces/:id", "routes/admin/workspaces.$id/route.tsx"),
      route("projects", "routes/admin/projects/route.tsx"), //    every project, observed read-only
      route("projects/:id", "routes/admin/projects.$id/route.tsx"),
      route("tasks", "routes/admin/tasks/route.tsx"), //      every task in the system, peekable
      route("bots", "routes/admin/bots/route.tsx"), //        every bot HEAD, observed read-only
      route("routines", "routes/admin/routines/route.tsx"),
      route("bots/:id", "routes/admin/bots.$id/route.tsx"),
      route("models", "routes/admin/models/route.tsx"), //    catalog · doctor · prices · routing
      route("spend", "routes/admin/spend/route.tsx"),
      route("audit", "routes/admin/audit/route.tsx"),
    ]),
  ]),
] satisfies RouteConfig;
