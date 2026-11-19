/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useEffect, useRef } from "react";
import { loadRfb, mountVnc, type VncChannel } from "~/lib/screen-vnc";
import { cn } from "~/lib/utils";

/** The lease holder's desktop, on a driver whose door serves a control stream (VNC): noVNC on
 *  the channel the screen socket feeds. A new channel (a fresh RFB stream) remounts it; the pane unmounts it the
 *  moment control ends. noVNC owns the pointer and the keyboard inside it. */
export function ScreenVnc({ channel, className }: { channel: VncChannel; className?: string }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    return mountVnc(el, channel, loadRfb);
  }, [channel]);
  return (
    <div
      ref={ref}
      role="application"
      aria-label="The remote desktop: you are driving"
      className={cn("h-full w-full overflow-hidden", className)}
    />
  );
}
