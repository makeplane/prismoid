/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { redirect } from "react-router";
import { api, clearSession, unwrap } from "~/lib/api/client";

// Logout is POST-only. We revoke server-side first (best-effort - the token is still attached), then
// clear the local session no matter what, and land on /login.
export async function clientAction() {
  try {
    await unwrap(api.POST("/v1/auth/logout", {}));
  } catch {
    // ignore - a failed revoke must not trap the user in a session they asked to end.
  }
  clearSession();
  return redirect("/login");
}

// A stray GET to /logout just goes to the sign-in screen.
export function clientLoader() {
  return redirect("/login");
}
