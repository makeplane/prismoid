/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The transcript's line while the task is paused (ADR-064). A step waiting for its device
// is a pause nobody asked for: it reads "Waiting for <device name>",
// the device-away gate's own title, never "Paused".

import { cn } from "~/lib/utils";
import { pauseStatusLine, type PauseView } from "./progress-copy";

export function PauseBanner({
  pause,
  away,
  className,
}: {
  pause: PauseView | null;
  /** The open device-away gate's title ("Waiting for MacBook Pro"), else null. */
  away?: string | null;
  className?: string;
}) {
  return (
    <div className={cn("text-muted-foreground text-body-sm-regular", className)} role="status">
      {away ?? pauseStatusLine(pause)}
    </div>
  );
}
