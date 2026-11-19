/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import type { ReactNode } from "react";

/** The section-tab scaffold: a tab's standing controls ride the topbar;
 *  in-page, the min-h-8 control row renders ONLY when given something to say (active
 *  chips, a selection bar) - the component enforces "the row must earn its band". Content
 *  stacks at ONE rhythm. TaskBrowser speaks the same anatomy internally; simple tabs
 *  compose this. */
export function SectionTab({ controls, children }: { controls?: ReactNode; children: ReactNode }) {
  return (
    <div className="space-y-4">
      {controls != null && (
        <div className="flex min-h-8 flex-wrap items-center gap-2">{controls}</div>
      )}
      {children}
    </div>
  );
}
