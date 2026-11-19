/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { type RouteConfig, index, layout, route } from "@react-router/dev/routes";

// ─────────────────────────────────────────────────────────────────────────────
// THE APP MAP. Read top-to-bottom: this is the entire application in one file.
// Public auth routes sit outside the shell. Everything else lives inside the
// protected shell layout - its clientLoader becomes the auth guard in F1.
// ─────────────────────────────────────────────────────────────────────────────
export default [
  route("login", "routes/login/route.tsx"),
  route("signup", "routes/signup/route.tsx"),
  route("forgot-password", "routes/forgot-password/route.tsx"),
  route("reset-password", "routes/reset-password/route.tsx"),
  // An emailed verification link: the code rides the URL fragment; signed out it waits through sign-in.
  route("verify-email", "routes/verify-email/route.tsx"),
  route("logout", "routes/logout/route.tsx"),
  // An emailed workspace invite (spec 9): logged out it goes on to signup, logged in it accepts.
  route("invite/:code", "routes/invite.$code/route.tsx"),
  // A Project join link: logged out it goes to login and back, logged in it accepts.
  route("join/:code", "routes/join.$code/route.tsx"),
  // The template preview: a share link must open logged out or it is not a
  // share link. Anonymous read-only; the CTA routes through /login?next= for strangers.
  route("t/bots/:slug", "routes/t.bots.$slug/route.tsx"),

  layout("routes/shell/route.tsx", [
    // Full-bleed screens sit outside the standard page container: the task view's
    // column+rail split owns the whole width, and the artifact screen sizes ITSELF -
    // app-frame kinds (collections) take the exact viewport remainder with no page scroll,
    // documents reproduce the container's page chrome locally.
    route("tasks/:id", "routes/tasks.$id/route.tsx"), //        live task view (the flagship)
    // The SAME page module under its Project: a Project task's canonical URL. The
    // loader redirects whichever of the two spellings is not the task's own, and the segment
    // never authorizes (the server reads by task id), so a wrong one only redirects.
    route("projects/:projectId/tasks/:id", "routes/tasks.$id/route.tsx", {
      id: "routes/projects.$projectId.tasks.$id/route",
    }),
    route("artifacts/:id", "routes/artifacts.$id/route.tsx"), // artifact + verification report
    route("inbox", "routes/inbox/route.tsx"), //                 Linear-style two-pane inbox
    route("bots/:name?", "routes/bots/route.tsx"), //           two-pane bots messenger

    layout("routes/shell/container.tsx", [
      index("routes/home/route.tsx"), //          /            the landing - describe, guard, send
      route("tasks", "routes/tasks/route.tsx"), //  /tasks      list
      route("routines", "routes/routines/route.tsx"), // standing work: list + peek
      route("projects", "routes/projects/route.tsx"),
      route("projects/:id", "routes/projects.$id/route.tsx"),
      route("connectors", "routes/connectors/route.tsx"), // the service catalog + permissions
      route("allowed-sites", "routes/allowed-sites/route.tsx"), // standing egress grants
      // The operator console is NOT here - it is its own application (routes.admin.ts),
      // built separately and served on its own domain. This bundle never ships admin code.
    ]),
  ]),

  // The design system, whole - tokens (the radius knob, live), kit, registry, AI layer,
  // renderers, and the full component census. Deliberately linked from NOWHERE: /ds is an
  // easter egg you type, not a screen you reach. Replaces the old /dev/kit gallery.
  route("ds", "routes/ds/route.tsx"),
] satisfies RouteConfig;
