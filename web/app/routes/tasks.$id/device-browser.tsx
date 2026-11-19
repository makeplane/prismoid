/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// A device Machine's browser: a window in the Prismoid Chrome on the
// user's own computer. There is no screen to show here - the person is looking at the real
// window - so where a cloud Machine shows its screen, this says where the browser is.

import { AppWindow } from "lucide-react";
import { browserOn } from "~/lib/machine-copy";

export function DeviceBrowser({ name }: { name?: string | null }) {
  return (
    <div className="bg-muted/40 flex items-center gap-2 rounded-md border px-3 py-2.5 text-body-sm-regular">
      <AppWindow aria-hidden className="text-muted-foreground size-4 shrink-0" />
      <span className="min-w-0 truncate">{browserOn(name)}</span>
    </div>
  );
}
