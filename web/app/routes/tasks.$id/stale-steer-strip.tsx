/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The stale-steer strip (ADR-064). A steer stamps the plan epoch the screen showed when its
// author began typing; if the task moved on meanwhile (someone else steered, or a new turn
// started) the door answers 409 `stale_steer`, naming what was missed. The draft stays in the
// box, and this strip, above it, says what changed and offers the two ways on: send it anyway
// against the plan as it stands now, or let it go.

import { Button } from "@makeplane/propel/components/button";
import type { MissedSteer } from "~/lib/api/client";
import { cn } from "~/lib/utils";

/** A refused steer: the words that were not sent, and what their author had not seen. */
export type StaleSteer = { text: string; missed: MissedSteer[] };

export function StaleSteerStrip({
  stale,
  busy = false,
  onSendAnyway,
  onDiscard,
  className,
}: {
  stale: StaleSteer;
  busy?: boolean;
  onSendAnyway: () => void;
  onDiscard: () => void;
  className?: string;
}) {
  return (
    <div
      role="alert"
      className={cn(
        "bg-popover ring-foreground/10 space-y-2 rounded-xl px-3 py-2 text-body-sm-regular shadow-(--shadow-overlay) ring-1",
        className,
      )}
    >
      {stale.missed.length === 0 ? (
        <p className="text-muted-foreground">The task moved on while you were typing.</p>
      ) : (
        <ul className="space-y-0.5">
          {stale.missed.map((m, i) => (
            <li key={`${m.kind}-${m.epoch}-${i}`} className="text-muted-foreground">
              <span className="text-foreground font-medium">{m.author}</span>
              {m.kind === "turn"
                ? " started the next turn"
                : ` steered while you were typing: '${m.text ?? ""}'`}
            </li>
          ))}
        </ul>
      )}
      <div className="flex items-center justify-end gap-2">
        <Button
          variant="ghost"
          size="sm"
          stretch="auto"
          label="Discard"
          disabled={busy}
          onClick={onDiscard}
        />
        <Button
          variant="primary"
          size="sm"
          stretch="auto"
          label="Send anyway"
          disabled={busy}
          onClick={onSendAnyway}
        />
      </div>
    </div>
  );
}
