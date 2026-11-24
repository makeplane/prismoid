/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// Start-then-poll for the authoring jobs (Draft for me, Test evals): both run on the worker and
// land back on the bot's rows, so the tab re-reads them every few seconds until the rows' mark
// (authoring-mark.ts) moves off what it was at the click, or the cap passes. ONE interval, cleared
// on unmount and restarted (not stacked) by a second click.
import { useEffect, useState } from "react";
import { AUTHORING_CAP_MS } from "./authoring-mark";

const EVERY_MS = 5000;

/** Returns `start`: call it once the job is accepted. */
export function useAuthoringPoll(mark: string, revalidate: () => void): () => void {
  const [waiting, setWaiting] = useState<{ mark: string; until: number } | null>(null);
  useEffect(() => {
    if (!waiting) return;
    if (mark !== waiting.mark) {
      setWaiting(null); // the job landed
      return;
    }
    const timer = window.setInterval(() => {
      if (Date.now() >= waiting.until) setWaiting(null);
      else revalidate();
    }, EVERY_MS);
    return () => window.clearInterval(timer);
  }, [waiting, mark, revalidate]);
  return () => setWaiting({ mark, until: Date.now() + AUTHORING_CAP_MS });
}
