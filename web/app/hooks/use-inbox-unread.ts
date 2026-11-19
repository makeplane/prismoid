/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The sidebar's unread count as the product shell's loader last read it (GET /v1/inbox), the
// number the Inbox badge shows. `undefined` outside the shell. A page that learns the count moved
// (it read items, or the server read them when a gate closed) re-runs the loaders to refresh it.
// Typed here rather than imported from the shell's route module: nothing outside routes/ imports one.

import { useRouteLoaderData } from "react-router";

export function useInboxUnread(): number | undefined {
  const shell = useRouteLoaderData("routes/shell/route") as { unread?: number } | undefined;
  return shell?.unread;
}
