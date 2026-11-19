/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// Every task in the system - the operator's flight radar, which is the shared TaskConsole
// (components/admin/task-console) in its global home: search rides the topbar, and the
// user_id/project_id URL params scope by link from other pages (the user chip wears the
// account's email, not "one user"). The peek is URL-borne (?task=) so a row, a refresh,
// and an audit link all open the same drawer in place.

import { TaskConsole } from "~/components/admin/task-console";

export function meta() {
  return [{ title: "Tasks · Admin · Prismoid" }];
}

export default function AdminTasks() {
  return <TaskConsole topbarSearch />;
}

export { RouteErrorBoundary as ErrorBoundary } from "~/components/layout/route-error";
