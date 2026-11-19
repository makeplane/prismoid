/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// Proactively sign out just before the session token expires, so a request never fails
// mid-flight. If the expiry is unknown we simply rely on the 401 handler instead.
//
// ONE implementation, two shells: the product's (routes/shell) and the operator console's
// (routes/admin-shell) are different applications with different chrome, but this is not
// chrome - it is the session contract, and it was copied verbatim between them. A second copy
// is how one of the two quietly stops signing people out.

import { useEffect } from "react";
import { toast } from "@prismoid/blocks/toast";
import { clearSession } from "~/lib/api/client";
import { loginHref, msUntilExpiry } from "~/lib/auth";

// setTimeout clamps to a 32-bit signed millisecond count; anything larger fires immediately.
const MAX_TIMEOUT_MS = 2_147_483_000;
// Leave before the token dies, not as it dies - an in-flight request needs the margin.
const MARGIN_MS = 10_000;

export function useExpiryTimer(): void {
  useEffect(() => {
    const ms = msUntilExpiry();
    if (ms == null) return;
    const logout = () => {
      clearSession();
      toast("Session expired", { description: "Please sign in again." });
      window.location.href = loginHref(window.location.pathname + window.location.search);
    };
    if (ms <= 0) {
      logout();
      return;
    }
    const id = setTimeout(logout, Math.min(Math.max(0, ms - MARGIN_MS), MAX_TIMEOUT_MS));
    return () => clearTimeout(id);
  }, []);
}
