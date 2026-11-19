/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/** The task browser, split by concern (one file had grown too large). The public
 *  surface is unchanged - consumers import "~/components/tasks/task-browser" as before. */

export {
  fetchTaskCounts,
  fetchTasksPage,
  readBrowserParams,
  TASK_GROUPS,
  type TaskBrowserSeed,
  type TaskFilters,
  type TaskQueryScope,
} from "./data";
export { TaskBrowser } from "./browser";
