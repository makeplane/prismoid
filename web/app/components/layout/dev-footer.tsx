/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useEffect, useState } from "react";
import { API_PROTOCOL, API_URL, ping } from "~/lib/api/client";
import { cn } from "~/lib/utils";

/** A quiet dev-only strip: is the API up, which one, and the protocol we speak. */
export function DevFooter() {
  const [up, setUp] = useState<boolean | null>(null);
  useEffect(() => {
    // The strip draws in dev only, so only dev pings: a production tab asks nothing.
    if (!import.meta.env.DEV) return;
    let live = true;
    const tick = () => ping().then((ok) => live && setUp(ok));
    tick();
    const id = setInterval(tick, 10_000);
    return () => {
      live = false;
      clearInterval(id);
    };
  }, []);

  if (!import.meta.env.DEV) return null;
  return (
    // Bottom-RIGHT: the bottom-left corner is the sidebar's account chip, and the strip sat on
    // the person's name and email. Nothing lives in this corner but toasts, which stack above
    // it (z-60) for the seconds they last. pointer-events-none: a status strip never eats a
    // click meant for the page under it.
    <div className="text-muted-foreground/70 pointer-events-none fixed right-2 bottom-2 z-50 flex items-center gap-2 rounded-pill border bg-background/80 px-2.5 py-1 select-none text-caption-md-regular backdrop-blur">
      <span
        className={cn(
          "size-1.5 rounded-full",
          up === null ? "bg-muted-foreground" : up ? "bg-success" : "bg-destructive",
        )}
        title={up === null ? "checking…" : up ? "API up" : "API unreachable"}
      />
      <span className="tabular-nums">{API_URL.replace(/^https?:\/\//, "")}</span>
      <span className="opacity-50">·</span>
      <span>protocol {API_PROTOCOL}</span>
    </div>
  );
}
