/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useScreen, type ScreenStatus } from "~/hooks/use-screen";
import { cn } from "~/lib/utils";
import { ScreenCanvas } from "./screen-canvas";

/** What the screen says when it has no live picture to show. */
export const SCREEN_STATUS_LINE: Record<ScreenStatus, string> = {
  connecting: "Connecting",
  live: "",
  starting: "Starting the browser",
  stopped: "The browser is stopped. It starts with the agent's next step.",
  no_screen: "This Machine has no live screen yet.", // a driver with no screen door
  asleep: "Asleep",
  off: "Computer use is off.",
  closed: "The screen closed.",
};

/** The status as a settled task shows it: nothing is "starting" once the task has ended, so a
 *  socket that never got a picture reads as stopped. */
export function shownStatus(status: ScreenStatus, settled: boolean): ScreenStatus {
  return settled && status === "starting" ? "stopped" : status;
}

/** The thumbnail's one line under the picture. Pure, so it is tested without a DOM. */
export function thumbLine(p: {
  status: ScreenStatus;
  masked: string | null;
  holderName: string | null;
}): string {
  if (p.masked !== null) return `${p.masked || "Someone"} is signing in privately`;
  if (p.status !== "live") return SCREEN_STATUS_LINE[p.status];
  if (p.holderName === null) return "Live · agent driving";
  return `${p.holderName || "Someone"} is driving`;
}

/**
 * The live thumbnail in the rail's Machine section: its own screen socket at 1 fps,
 * paused while the tab is hidden (the hook does that), the holder's name under it, and during a
 * private lease the last frame from before the claim, dimmed, with "{name} is signing in
 * privately". Clicking it opens the screen in the split pane.
 */
export function ScreenThumb({
  taskId,
  holderName,
  settled = false,
  onOpen,
}: {
  taskId: string;
  holderName: string | null;
  /** The task has ended: "starting" reads as stopped. */
  settled?: boolean;
  onOpen: () => void;
}) {
  const screen = useScreen(taskId, { pane: false, enabled: true });
  const status = shownStatus(screen.status, settled);
  return (
    <button
      type="button"
      onClick={onOpen}
      aria-label="Open the screen"
      title="Open the screen"
      className="group block w-full text-left"
    >
      <span className="bg-muted/40 relative block overflow-hidden rounded-md border">
        <ScreenCanvas
          frame={screen.frame}
          size={screen.size}
          aria-hidden
          className={cn(
            "block h-auto w-full transition-opacity",
            screen.masked !== null && "opacity-40",
            !screen.frame && "opacity-0",
          )}
        />
        {!screen.frame && (
          <span className="text-muted-foreground absolute inset-0 flex items-center justify-center text-caption-md-regular">
            {SCREEN_STATUS_LINE[status]}
          </span>
        )}
      </span>
      <span className="text-muted-foreground group-hover:text-foreground mt-1.5 block truncate text-caption-md-regular">
        {thumbLine({ status, masked: screen.masked, holderName })}
      </span>
    </button>
  );
}
