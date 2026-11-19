/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getToken } from "~/lib/api/client";
import { ALL_EVENT_TYPES } from "~/lib/api/events";
import { createPresence, emptyPresence, mintConn, type PresenceState } from "~/lib/presence";
import { taskFrameLog, resumeSeq } from "~/lib/frame-log";
import { foldFrame, foldKept } from "~/lib/stream-frame";
import { isStalled, systemWaitLimitMs } from "~/lib/stream-stall";
import {
  isFinalRefusal,
  nextReopenWait,
  REOPEN_FIRST_MS,
  taskStreamUrl,
} from "~/lib/stream-ticket";
import { initialView, type TaskView } from "~/lib/task-view";
import type { components } from "~/lib/api/types.gen";

type TaskDetailOut = components["schemas"]["TaskDetailOut"];

export type Stream = {
  view: TaskView;
  connected: boolean;
  stalled: boolean;
  /** Silent past the server's stated bound on a wait for the Machine (never true without one). */
  stalledLong: boolean;
  /** Fold local (optimistic) state through the same reducer pipeline - the follow-up bubble
   *  paints before the wire answers, and the durable `task.continued` frame reconciles it. */
  foldLocal: (fn: (v: TaskView) => TaskView) => void;
  /** Who is on the task right now - folded from the presence frames, which never
   *  reach the reducer. Always the LATEST state (every presence frame publishes it), so read it
   *  through `presentPeople` / `typingPeople` with `Date.now()` on any render. Renders are
   *  driven by frames and by a 1 s clock that fires only when a person or a typing line lapses
   *  (see `createPresence`). */
  presence: PresenceState;
  /** This tab's own connection id, sent as `?conn=` on the stream and on every typing POST. */
  conn: string;
  /** The task is no longer this viewer's to read (a 404/403 on the ticket mint: removed from its
   *  Project, or deleted). Final: the stream stops reopening, and the page revalidates. */
  gone: boolean;
};

/**
 * PROMOTED from routes/tasks.$id: the bot page's chat tab folds the same stream
 * through the same reducer - the typed-twice rule applied to a hook. Route-agnostic by
 * construction: it takes a task id and its detail seed, nothing of either page.
 *
 * The heart of F4. Opens the durable+ephemeral SSE (`?since=<newest seq this tab holds, else 0>`)
 * with a single-use stream ticket (lib/stream-ticket.ts: the session token never rides the URL),
 * folds every frame through the pure reducer, and coalesces bursts into one render per animation
 * frame. A ticket opens one stream once, so the browser's own retry would carry a spent one: on a
 * drop the hook closes the source and reopens it with a fresh ticket from the newest durable seq
 * it holds, backing off; the reducer's seq dedup makes any replay idempotent.
 *
 * The socket lives until unmount - NEVER closed on a terminal frame. A settled task is a
 * conversation at rest, not a dead one: the next follow-up's `task.continued` and everything after it
 * ride this same stream, so closing on `task.delivered` would cut the thread off exactly when the
 * user continues it. (The server never hangs up a follow stream; 15 s pings keep proxies alive; an
 * idle EventSource on a settled page is the accepted cost of a page that can always continue.)
 */
export function useTaskStream(taskId: string, detail: TaskDetailOut): Stream {
  const [view, setView] = useState<TaskView>(() => initialView(detail));
  const [connected, setConnected] = useState(false);
  const [stalled, setStalled] = useState(false);
  const [stalledLong, setStalledLong] = useState(false);
  const [presence, setPresence] = useState<PresenceState>(emptyPresence);
  const [gone, setGone] = useState(false);
  // One id per open tab per task: the server validates it ([A-Za-z0-9_-]{8,64}) and echoes it
  // on every presence frame this stream causes. Never an identity - see lib/presence.ts.
  const conn = useMemo(
    () => mintConn(),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `taskId` is not read inside: it is the KEY, a new task is a new tab-connection
    [taskId],
  );

  const viewRef = useRef(view);
  // `detail` is the SEED only - read through a ref so a revalidation (Rename, a settings
  // save) that mints a fresh `detail` object never re-runs the effect below. Depending on
  // the object tore the SSE socket down and wiped the shown ephemeral text mid-stream.
  // The socket must outlive every revalidate and only re-subscribe per task.
  const detailRef = useRef(detail);
  detailRef.current = detail;
  const lastEventAt = useRef(Date.now());
  const rafScheduled = useRef(false);
  const foldRef = useRef<(fn: (v: TaskView) => TaskView) => void>(() => {});

  useEffect(() => {
    const token = getToken() ?? "";
    // A return to this task folds the frames this tab already holds and asks only for what
    // came after them (lib/frame-log.ts); a first visit starts from 0.
    const kept = taskFrameLog.open(taskId, token);
    viewRef.current = foldKept(initialView(detailRef.current), kept);
    setView(viewRef.current);
    let keptSeq = resumeSeq(kept);
    // The publisher lives in the effect; it hands React the latest presence on every
    // presence frame, and on the clock only when something lapses.
    const publisher = createPresence(setPresence);
    publisher.reset();

    let source: EventSource | null = null;
    let closed = false;
    let reopenTimer: number | undefined;
    let reopenWait = REOPEN_FIRST_MS;

    const flush = () => {
      rafScheduled.current = false;
      setView(viewRef.current);
    };
    const schedule = () => {
      if (rafScheduled.current) return;
      rafScheduled.current = true;
      requestAnimationFrame(flush);
    };

    foldRef.current = (fn) => {
      viewRef.current = fn(viewRef.current);
      schedule();
    };

    const onFrame = (e: MessageEvent) => {
      // One pure fold decides where a frame goes (lib/stream-frame.ts): presence into its own
      // state, everything else into the thread, and only the thread's frames move the stall
      // clock. This stays thin on purpose - the rule lives where it can be tested.
      const now = Date.now();
      // Only durable frames carry an SSE id (their seq), and the browser repeats the last id on
      // the id-less frames after it, so a frame is durable exactly when its id moves forward.
      const seq = Number(e.lastEventId);
      if (seq > keptSeq) {
        keptSeq = seq;
        taskFrameLog.keep(taskId, token, { type: e.type, data: e.data, seq });
      }
      const r = foldFrame(
        { view: viewRef.current, presence: publisher.get(), lastSignalAt: lastEventAt.current },
        e.type,
        e.data,
        now,
      );
      lastEventAt.current = r.lastSignalAt;
      if (r.alive) setStalled(false);
      if (r.changed === "presence") {
        publisher.frame(r.presence, now);
      } else if (r.changed === "view") {
        viewRef.current = r.view;
        schedule();
      }
    };

    const reopenLater = () => {
      reopenTimer = window.setTimeout(open, reopenWait);
      reopenWait = nextReopenWait(reopenWait);
    };
    const attach = (url: string) => {
      if (closed) return;
      const s = new EventSource(url);
      for (const type of ALL_EVENT_TYPES) s.addEventListener(type, onFrame as EventListener);
      s.onopen = () => {
        reopenWait = REOPEN_FIRST_MS;
        lastEventAt.current = Date.now();
        setConnected(true);
        setStalled(false);
      };
      s.onerror = () => {
        // The browser would retry with the spent ticket; reopen with a fresh one instead, unless
        // this unmount deliberately closed the socket.
        s.close();
        if (closed) return;
        setConnected(false);
        reopenLater();
      };
      source = s;
    };
    function open() {
      // A failed mint (a network blip, a restarting API) backs off like a dropped stream. A dead
      // session's 401 has already signed the tab out (lib/api/client.ts).
      // A refusal that will answer the same forever (the task is not this viewer's any more) ends
      // the stream instead.
      taskStreamUrl(taskId, keptSeq, conn).then(attach, (err: unknown) => {
        if (closed) return;
        if (isFinalRefusal(err)) setGone(true);
        else reopenLater();
      });
    }
    open();

    // Watchdog: while the task is live, flag prolonged silence so the UI never dies quietly.
    const watchdog = window.setInterval(() => {
      const now = Date.now();
      setStalled(isStalled(viewRef.current.status, lastEventAt.current, now));
      const limit = systemWaitLimitMs(viewRef.current.feed);
      setStalledLong(
        limit != null && isStalled(viewRef.current.status, lastEventAt.current, now, limit),
      );
    }, 3000);
    // The TTLs expire on screen: a tab that died without a leave lapses at 30 s, a typing
    // line at 6 s, with no frame to say so.
    const presenceClock = window.setInterval(() => publisher.tick(Date.now()), 1000);

    setGone(false);
    return () => {
      closed = true;
      source?.close();
      window.clearTimeout(reopenTimer);
      window.clearInterval(watchdog);
      window.clearInterval(presenceClock);
      foldRef.current = () => {};
    };
  }, [taskId, conn]);

  const foldLocal = useCallback((fn: (v: TaskView) => TaskView) => {
    foldRef.current(fn);
  }, []);

  return { view, connected, stalled, stalledLong, foldLocal, presence, conn, gone };
}
