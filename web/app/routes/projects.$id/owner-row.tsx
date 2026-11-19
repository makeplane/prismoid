/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The Project owner as one row of People. The owner is a column on the Project, never a member row
//, so the members list cannot name them; the Project read carries them instead and
// this draws them the way a member is drawn, with the role "owner" where a member's role sits.

import { initials } from "~/components/kit";
import { Badge } from "@makeplane/propel/components/badge";
import { personName } from "~/lib/format";

/** `email` is null for a workspace guest's read: a guest knows people by name only. */
export function OwnerRow({ owner }: { owner: { name: string; email: string | null } }) {
  const name = personName(owner.name || owner.email);
  return (
    <li className="flex items-center gap-3 px-4 py-3">
      <span className="bg-muted text-muted-foreground flex size-8 shrink-0 items-center justify-center rounded-full text-caption-md-regular font-medium">
        {initials(name)}
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-body-sm-regular font-medium">{name}</span>
        {owner.name && owner.email && (
          <span className="text-muted-foreground block truncate text-caption-md-regular">
            {owner.email}
          </span>
        )}
      </span>
      <Badge variant="neutral" size="xs" label="Owner" />
    </li>
  );
}
