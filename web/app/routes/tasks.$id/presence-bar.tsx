/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The task page's presence chrome (ADR-062): who is here (the header stack), who is typing
// (one muted line above the composer), and what a viewer sees where the composer would be.
// All three are pure views over lib/presence.ts - the fold and the clocks live there.

import { AvatarStack } from "~/components/kit";
import { typingLine, type PresentPerson, type TypingPerson } from "~/lib/presence";
import { cn } from "~/lib/utils";

/** Who has the task open right now: editors in full, viewers dimmed as "watching". You alone
 *  draw nothing - a personal task's header stays as it was, and a stack of one says nothing. */
export function PresenceBar({ people }: { people: readonly PresentPerson[] }) {
  if (people.length < 2) return null;
  return (
    <AvatarStack
      label={`${people.length} people here`}
      people={people.map((p) => ({ id: p.id, name: p.name, dim: !p.editor }))}
    />
  );
}

/** "Ada is typing…", "Bob is steering…", "Ada and Bob are typing…" - above the composer,
 *  polite and quiet. The live region stays MOUNTED and only its text changes: a region that
 *  appears already holding its text is not announced by assistive tech. While silent it is
 *  `sr-only`, so nothing shows on screen. */
export function TypingLine({ people }: { people: readonly TypingPerson[] }) {
  const line = typingLine(people);
  return (
    <p
      role="status"
      aria-live="polite"
      className={cn(
        "text-muted-foreground truncate text-caption-md-regular",
        line ? "mb-1.5 px-1" : "sr-only",
      )}
    >
      {line}
    </p>
  );
}

/** Where a viewer's composer would be. They read along; only editors write. */
export function ViewerNote() {
  return (
    <p className="text-muted-foreground bg-popover ring-foreground/10 rounded-xl px-4 py-3 text-body-sm-regular shadow-(--shadow-overlay) ring-1">
      You&apos;re viewing. Ask the Project owner for edit access.
    </p>
  );
}
