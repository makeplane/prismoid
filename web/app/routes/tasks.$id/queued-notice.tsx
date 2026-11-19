/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/** Pre-machine phase - queued / busy / offline / unreachable. Never "working…". */

import { CircleAlert } from "lucide-react";
import { cn } from "~/lib/utils";
import { useWorkerLiveness } from "~/lib/worker-liveness";

/** The pre-machine phase, stated honestly. For 15 s it's "queued"; after that it asks
 *  /healthz whether the worker is even alive and says exactly which it is. A queued task must
 *  never impersonate a running one - that lie is how a stopped worker reads as an eternal
 *  "working…" (learned the hard way). */
export function QueuedNotice({ className }: { className?: string }) {
  const liveness = useWorkerLiveness(true, false);

  if (liveness === "offline") {
    return (
      <div
        className={cn(
          className,
          "text-muted-foreground flex items-start gap-2 text-body-sm-regular",
        )}
        role="status"
      >
        <CircleAlert className="mt-0.5 size-4 shrink-0" />
        <span>
          The machine is offline right now. This task is saved and will start on its own the moment
          it&apos;s back - nothing is lost.
        </span>
      </div>
    );
  }
  if (liveness === "unreachable") {
    return (
      <div
        className={cn(
          className,
          "text-muted-foreground flex items-start gap-2 text-body-sm-regular",
        )}
        role="status"
      >
        <CircleAlert className="mt-0.5 size-4 shrink-0" />
        <span>
          Can&apos;t reach the machine right now. This task is saved - try again in a moment, or
          check that the API is up.
        </span>
      </div>
    );
  }
  return (
    <div
      className={cn(className, "text-muted-foreground shimmer text-body-sm-regular")}
      role="status"
      aria-live="polite"
    >
      {liveness === "ok"
        ? "still queued - the machine is busy; starting shortly…"
        : "queued - waiting for the machine…"}
    </div>
  );
}
