/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/** Every localStorage key the app owns, in one greppable place (keys defined where used let
 *  a typo in one silently fork a preference). */
export const STORAGE = {
  token: "prismoid.token",
  expires: "prismoid.expires",
  lastProject: "prismoid.lastspace",
  taskRail: "prismoid.taskrail",
  homeRecent: "prismoid.home.recent",
  tasksView: "prismoid.tasks.view",
  drawerWidth: "prismoid.drawer.width",
  splitPct: "prismoid.split.pct", // the task page's split reader, as a % of its row
  navWidth: "prismoid.nav.width", // the site nav's dragged width, px
  inboxPane: "prismoid.inbox.pane",
  sidebarTasksGroup: "prismoid.sidebar.tasks.group",
  sidebarTasksMarks: "prismoid.sidebar.tasks.marks",
  collectionPeek: "prismoid.collection.peek",
  verifyCode: "prismoid.verify.code", // sessionStorage: the emailed code, across the sign-in detour
} as const;
