/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The signed-in person looking at the page, read from the product shell's loader - every
// product route renders inside it, and its clientLoader already validated the session against
// GET /v1/auth/me. `undefined` outside the shell; a caller treats an unknown viewer as nobody
// in particular (never as the task's creator).

import { useRouteLoaderData } from "react-router";
import type { User } from "~/lib/auth";
import type { clientLoader } from "~/routes/shell/route";

export function useViewer(): User | undefined {
  return useRouteLoaderData<typeof clientLoader>("routes/shell/route")?.user;
}
