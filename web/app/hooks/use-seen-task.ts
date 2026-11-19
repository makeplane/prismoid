/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { useRevalidator } from "react-router";
import { useInboxUnread } from "~/hooks/use-inbox-unread";
import { api, unwrap } from "~/lib/api/client";
import { SEEN_SETTLE_MS, badgeStale, shouldMarkSeen } from "~/lib/seen-task";

function shown(): boolean {
  return typeof document === "undefined" || document.visibilityState === "visible";
}

/**
 * The viewer has seen this task: their own inbox items about it read, on open (unless `onOpen` is
 * false) and on each piece of news while the view is shown (`key` moves: `seenKey`, `cardSeenKey`
 * in seen-task.ts), once the view has been still for SEEN_SETTLE_MS. Best-effort: a failed call
 * leaves the items unread, which is the safe side, and the next news tries again. When the badge
 * no longer matches what the server holds, the loaders re-run once they are idle: a refresh never
 * restarts one already loading.
 */
export function useSeenTask(taskId: string, key: string, { onOpen = true } = {}) {
  const [visible, setVisible] = useState(shown);
  useEffect(() => {
    const onVisibility = () => setVisible(shown());
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
  }, []);

  const revalidator = useRevalidator();
  const revalidatorRef = useRef(revalidator);
  revalidatorRef.current = revalidator;
  const badge = useRef<number | undefined>(undefined);
  badge.current = useInboxUnread();
  const owed = useRef(false); // the badge needs a re-read the loaders have not started yet
  const refresh = useCallback(() => {
    if (!owed.current || revalidatorRef.current.state !== "idle") return;
    owed.current = false;
    void revalidatorRef.current.revalidate();
  }, []);
  useEffect(refresh, [revalidator.state, refresh]);

  const reported = useRef<string | null>(onOpen ? null : key); // what it opened on counts as told
  useEffect(() => {
    if (!shouldMarkSeen(key, reported.current, visible)) return;
    const settle = setTimeout(() => {
      reported.current = key;
      unwrap(api.POST("/v1/inbox/tasks/{task_id}/read", { params: { path: { task_id: taskId } } }))
        .then((read) => {
          owed.current ||= badgeStale(read, badge.current);
          refresh();
        })
        .catch(() => {});
    }, SEEN_SETTLE_MS);
    return () => clearTimeout(settle);
  }, [key, visible, taskId, refresh]);
}
