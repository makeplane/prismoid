/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useCallback, useEffect, useMemo, useRef } from "react";
import { api } from "~/lib/api/client";
import { createTypingSender, typingMode, type TypingMode } from "~/lib/presence";
import type { GateView } from "~/lib/task-view";

/**
 * The composer's typing frames, as one hook: at most one `POST /presence` per
 * 1.5 s while keys land and a final `typing:false` 2 s after the last (the pure sender in
 * lib/presence.ts owns the timing and is unit-tested there).
 *
 * Returns the composer's text observer: pass every text change to it. It is STABLE - the
 * composer re-fires its `onTextChange` effect whenever the identity changes, and an unstable
 * one would re-arm the idle timer on every render and spam `typing:on`. It does nothing while
 * `enabled` is false: only editors of a SHARED task send frames (a personal task has no one
 * to tell, and the server 403s a viewer).
 *
 * The frame's `mode` is read through a ref at send time, so it always says what the box is
 * doing NOW - answering the open question, steering the live turn, or a follow-up - the
 * composer's own three branches (`typingMode`). Best-effort by contract: a failed frame is a
 * missing dot, never an error, and goes through `api.POST` directly (not `unwrap`) so it never
 * fires the 401 or 429 handlers or a toast.
 */
export function useTypingSender({
  taskId,
  conn,
  enabled,
  live,
  gate,
}: {
  taskId: string;
  conn: string;
  enabled: boolean;
  live: boolean;
  gate: Pick<GateView, "kind" | "questions"> | null;
}): (text: string) => void {
  const modeRef = useRef<TypingMode>("follow_up");
  modeRef.current = typingMode({ live, gate });
  const sender = useMemo(
    () =>
      createTypingSender((on) => {
        void api
          .POST("/v1/tasks/{task_id}/presence", {
            params: { path: { task_id: taskId } },
            body: { conn, typing: on, mode: modeRef.current },
          })
          .catch(() => {
            /* presence is a nicety */
          });
      }),
    [taskId, conn],
  );
  // an unmount, or losing the right to send mid-sentence, retracts what was announced
  useEffect(() => {
    if (!enabled) sender.stop();
    return () => sender.stop();
  }, [sender, enabled]);
  return useCallback(
    (text: string) => {
      if (enabled) sender.change(text);
    },
    [sender, enabled],
  );
}
