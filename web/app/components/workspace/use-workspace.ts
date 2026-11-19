/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useEffect, useState } from "react";
import { api, unwrap } from "~/lib/api/client";
import type { User } from "~/lib/auth";

/** The caller and the workspace this credential works in (`MeOut.workspace`) - what every workspace pane asks first. */
export function useMe(): User | null {
  const [me, setMe] = useState<User | null>(null);
  useEffect(() => {
    unwrap(api.GET("/v1/auth/me"))
      .then(setMe)
      .catch(() => {});
  }, []);
  return me;
}
