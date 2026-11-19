/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { Avatar } from "@makeplane/propel/components/avatar";
import { AvatarGroup } from "@makeplane/propel/components/avatar-group";
import { initials } from "./primitives";

export type StackPerson = {
  id: string;
  name: string;
  /** Watching, not working (a viewer): drawn at reduced opacity, titled "(watching)". */
  dim?: boolean;
};

/** Overlapping initials - who is there, at a glance. One stack for the Project header
 *  (its members) and the task header (who is here right now). Promoted from the Project page
 *  on its second consumer. Nobody is nothing: no empty ring. */
export function AvatarStack({
  people,
  max = 4,
  label,
}: {
  people: readonly StackPerson[];
  max?: number;
  label?: string;
}) {
  if (people.length === 0) return null;
  // Only the first `max` faces are handed in, with the true `total`, so the stack collapses
  // exactly past `max` (Propel alone would show a 5th face instead of "+1").
  const shown = people.slice(0, max);
  return (
    <AvatarGroup
      size="sm"
      max={max}
      total={people.length}
      // a group, not one image: each face keeps its own name for assistive tech
      role="group"
      label={label ?? `${people.length} ${people.length === 1 ? "person" : "people"}`}
    >
      {shown.map((p) => {
        // who it is (and that they are only watching) is the face's accessible name - a hover
        // title alone reaches nobody on touch or by keyboard; the initials are decoration
        const name = p.dim ? `${p.name} (watching)` : p.name;
        const face = <Avatar key={p.id} title={name} alt={name} fallback={initials(p.name)} />;
        return p.dim ? (
          <span key={p.id} className="inline-flex opacity-50">
            {face}
          </span>
        ) : (
          face
        );
      })}
    </AvatarGroup>
  );
}
