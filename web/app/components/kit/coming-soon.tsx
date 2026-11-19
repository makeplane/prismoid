/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { Hammer } from "lucide-react";
import { EmptyState, PageHeader } from "./primitives";

/**
 * ComingSoon - the placeholder for a screen that's wired into the app map but not yet built. Every
 * route is real from day one (the clickable map); this makes an unbuilt screen read as intentional,
 * and names the phase it arrives in. Never a stand-in for mocked data - only for a not-yet-live screen.
 */
export function ComingSoon({
  title,
  description,
  phase,
  as,
}: {
  title: string;
  description?: string;
  phase?: string;
  as?: "h1" | "h2";
}) {
  return (
    <div className="space-y-6">
      <PageHeader as={as} title={title} description={description} />
      <EmptyState
        icon={Hammer}
        title={phase ? `Arrives in ${phase}` : "Coming soon"}
        description="This screen is part of the app map; its live implementation lands in a later phase."
      />
    </div>
  );
}
