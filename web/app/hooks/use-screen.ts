/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { API_URL, ApiError, api, unwrap } from "~/lib/api/client";
import { mintConn } from "~/lib/presence";
import { freshPaneConn, holdPaneConn, paneConn } from "~/lib/screen-conn";
import { decode, encode, type Header } from "~/lib/screen-wire";
import { createVncChannel, type VncChannel } from "~/lib/screen-vnc";
import type { ScreenLease } from "~/lib/task-view";

/**
 * The screen socket: one WebSocket per hook instance onto
 * `GET /v1/tasks/{id}/screen`, admitted by a single-use 30 s ticket minted for THIS connection -
 * so the long-lived token never rides a URL, and every connect and every reconnect mints anew.
 *
 * The connection logic lives in `createScreenClient` (pure of React, tested with a fake
 * WebSocket); `useScreen` only hands its snapshots to React.
 *
 * - Rate: the URL says whether this is a pane (`&pane=1`), and the open hint asks for the most
 *   the role could get - the thumbnail 1 fps, the pane 30. The server allows `min(asked, cap)`,
 *   where the cap is 1 for a thumbnail, 5 for a pane, and 30 only on the lease holder's own
 *   connection - so a pane that asked for 5 would hold its driver at 5.
 * - A hidden tab sends `hint{paused: true}` and the server stops its frames.
 * - Frames decode to an ImageBitmap off the main thread; the one it replaces is closed.
 * - Close codes: 4410 "off" and 4404 "closed" are final, and drop the last frame (no stale
 *   picture under "Computer use is off."); anything else reconnects with a new ticket, backing
 *   off 0.5 s doubling to 10 s, reset once the relay serves (a frame, a status, a mask).
 * - "off" asks again only when told to (`recheckScreens`): the switch moves only with a server
 *   restart, and the page hears every restart as its task stream coming back.
 * - "starting" that lasts past STOPPED_AFTER_MS is "stopped": an idle Machine whose browser was
 *   stopped has none until the agent's next step starts it, however long the relay polls.
 * - "no_screen" is a Machine with no screen at all (a driver with no screen door):
 *   no picture, and no waiting turns it into anything else.
 * - `origin` is the page's origin as the sidecar states it in `status` (re-sent when the page
 *   navigates or moves to another tab): the pane's address line, never the agent's words.
 *   `unreachable` is the origin the page could not load (Chromium's own error page, which a
 *   refused proxy tunnel lands on), "" once a page loaded.
 * - The pane's connection id is kept per task in this tab (`paneConn`), so closing and reopening
 *   the pane is the same connection, never "another tab"; the thumbnail's is its own. The pane
 *   holds it with a Web Lock (`holdPaneConn`), and connects only once it is this tab's: a
 *   duplicated tab, which copied it, mints its own.
 * - A driver whose door serves a control stream (VNC): `cursor` messages are the real pointer
 *   (its shape an object URL, its place in frame pixels), and while this connection holds control
 *   the relay streams the desktop's VNC in `vnc` messages - `vnc` is the channel noVNC attaches to,
 *   opened by `vnc{open}` and ended by `vnc{closed}` or the socket dropping.
 * - `driver` is the relay's `role`: whether this connection is the one the lease lets drive (null
 *   until it says, and while reconnecting).
 */

export type ScreenStatus =
  | "connecting"
  | "live"
  | "asleep"
  | "starting"
  | "stopped"
  | "no_screen"
  | "off"
  | "closed";

export type CursorShape = { url: string; w: number; h: number; hotX: number; hotY: number };
/** The real pointer a driver sends: where it is, and its shape once one was sent. */
export type ScreenCursor = { x: number; y: number; shape: CursorShape | null };

export type ScreenSnapshot = {
  frame: ImageBitmap | null;
  size: { w: number; h: number } | null;
  status: ScreenStatus;
  /** The holder's name while a private lease masks this socket's frames. */
  masked: string | null;
  /** An agent's browser verb is in flight: the sidecar drops human input until it ends. */
  busy: boolean;
  /** The page's origin as the server states it; "" when the browser shows no web page, null
   *  when nothing is stated (yet, or with no picture). */
  origin: string | null;
  /** The origin the page could not load, as the server states it; "" when it loaded, null when
   *  nothing is stated. */
  unreachable: string | null;
  /** This connection drives (the relay's `role`); null until the server says. */
  driver: boolean | null;
  /** The lease holder's desktop stream, on a driver whose door serves a control stream (VNC): set
   *  while the relay streams VNC to this connection (between `vnc{open}` and `vnc{closed}`); null
   *  everywhere else. */
  vnc: VncChannel | null;
  /** The real cursor on the frames, from a driver that sends one; null elsewhere, and while
   *  masked. */
  cursor: ScreenCursor | null;
};

export type ScreenClient = {
  start(): void;
  stop(): void;
  send(header: Header, payload?: Uint8Array): void;
};

export const SCREEN_FPS = { pane: 30, thumb: 1 } as const;
export const BACKOFF_MS = { first: 500, max: 10_000 } as const;
export const CLOSE_CODE = { badTicket: 4401, notFound: 4404, tooSlow: 4408, off: 4410 } as const;

// The running clients' rechecks: `recheckScreens` reaches every screen this tab shows.
const running = new Set<() => void>();

/** The page's task stream came back - the server restarted, and a restart is the only way
 *  `PRISMOID_COMPUTER_USE_ENABLED` changes - so every screen that said "off" asks again: one
 *  ticket each, answered 409 again while the switch is still off. */
export function recheckScreens(): void {
  for (const recheck of running) recheck();
}
/** "starting" this long is a browser that is stopped, not one that is starting (UI check O7). */
export const STOPPED_AFTER_MS = 5000;

export const IDLE_SCREEN: ScreenSnapshot = {
  frame: null,
  size: null,
  status: "connecting",
  masked: null,
  busy: false,
  origin: null,
  unreachable: null,
  driver: null,
  vnc: null,
  cursor: null,
};

export function screenUrl(taskId: string, ticket: string, pane: boolean): string {
  const base = `${API_URL.replace(/^http/, "ws")}/v1/tasks/${taskId}/screen`;
  return `${base}?ticket=${encodeURIComponent(ticket)}${pane ? "&pane=1" : ""}`;
}

async function mintTicket(taskId: string, conn: string): Promise<string> {
  const out = await unwrap(
    api.POST("/v1/tasks/{task_id}/screen/ticket", {
      params: { path: { task_id: taskId } },
      body: { conn },
    }),
  );
  return out.ticket;
}

/** The `masked` message names the holder, or says `null` when the mask lifts. A holder with no
 *  name (the server fails closed with an unnamed one) is still a mask: "" is not null. A bare
 *  string is tolerated as a name. */
function maskOf(h: Header): string | null {
  const v = h.holder;
  if (typeof v === "string") return v;
  if (v && typeof v === "object")
    return typeof (v as { name?: unknown }).name === "string" ? (v as { name: string }).name : "";
  return null;
}

function tabHidden(): boolean {
  return typeof document !== "undefined" && document.visibilityState === "hidden";
}

export function createScreenClient(opts: {
  taskId: string;
  conn: string;
  pane: boolean;
  onState: (s: ScreenSnapshot) => void;
}): ScreenClient {
  let state: ScreenSnapshot = IDLE_SCREEN;
  let socket: WebSocket | null = null;
  let stopped = true;
  let attempt = 0; // a stale ticket mint or socket of an earlier attempt is ignored
  let backoff: number = BACKOFF_MS.first;
  let retry: ReturnType<typeof setTimeout> | null = null;
  let decoding = 0; // frames that finish decoding out of order are dropped
  let shown = 0;
  // A frame whose decode began before the picture was masked or dropped (asleep, off, a reset's
  // new incarnation) is never drawn, nor does it say "live" over the state that dropped it.
  let gen = 0;
  let stalling: ReturnType<typeof setTimeout> | null = null; // "starting" turning into "stopped"

  const publish = (patch: Partial<ScreenSnapshot>) => {
    if (stopped) return;
    state = { ...state, ...patch };
    if (state.status !== "starting" && stalling) {
      clearTimeout(stalling);
      stalling = null;
    } else if (state.status === "starting" && !stalling) {
      stalling = setTimeout(() => {
        stalling = null;
        if (state.status === "starting") publish({ status: "stopped" });
      }, STOPPED_AFTER_MS);
    }
    opts.onState(state);
  };

  let shapeUrl: string | null = null; // the cursor shape's object URL, revoked when replaced

  const sendNow = (header: Header, payload?: Uint8Array) => {
    if (socket?.readyState === WebSocket.OPEN) socket.send(encode(header, payload));
  };

  const dropCursor = (): Partial<ScreenSnapshot> => ({ cursor: null });
  const endVnc = (): Partial<ScreenSnapshot> => {
    state.vnc?.end();
    return { vnc: null };
  };
  const onCursor = (h: Header, payload: Uint8Array<ArrayBuffer>) => {
    const whole = (v: unknown) =>
      Number.isInteger(v) && (v as number) >= 0 && (v as number) <= 4096 ? (v as number) : null;
    const prev = state.cursor ?? { x: 0, y: 0, shape: null };
    if (payload.byteLength > 0) {
      const w = whole(h.w),
        ht = whole(h.h);
      if (!w || !ht) return;
      if (shapeUrl) URL.revokeObjectURL(shapeUrl);
      shapeUrl = URL.createObjectURL(new Blob([payload], { type: "image/png" }));
      publish({
        cursor: {
          ...prev,
          shape: { url: shapeUrl, w, h: ht, hotX: whole(h.hot_x) ?? 0, hotY: whole(h.hot_y) ?? 0 },
        },
      });
      return;
    }
    const x = whole(h.x),
      y = whole(h.y);
    if (x === null || y === null) return;
    publish({ cursor: { ...prev, x, y } });
  };
  const onVnc = (h: Header, payload: Uint8Array<ArrayBuffer>) => {
    if (h.state === "open") {
      state.vnc?.end();
      return publish({ vnc: createVncChannel((p) => sendNow({ t: "vnc" }, p)) });
    }
    if (h.state === "closed") return publish(endVnc());
    if (payload.byteLength > 0) state.vnc?.push(payload);
  };

  const onVisibility = () => sendNow({ t: "hint", paused: tabHidden() });

  /** The picture goes, and the address it showed with it. */
  const dropFrame = (): Partial<ScreenSnapshot> => {
    gen++;
    state.frame?.close();
    return { frame: null, size: null, origin: null, unreachable: null, cursor: null };
  };

  const reconnect = () => {
    if (stopped) return;
    publish({ status: "connecting", busy: false, driver: null, ...endVnc() });
    const wait = backoff;
    backoff = Math.min(backoff * 2, BACKOFF_MS.max);
    retry = setTimeout(() => {
      retry = null;
      void open();
    }, wait);
  };

  const draw = (payload: Uint8Array<ArrayBuffer>, h: Header) => {
    const seq = ++decoding;
    const mine = gen;
    const sane = (n: unknown) =>
      Number.isInteger(n) && (n as number) >= 1 && (n as number) <= 4096 ? (n as number) : 0;
    const w = sane(h.w);
    const ht = sane(h.h);
    createImageBitmap(new Blob([payload], { type: "image/jpeg" })).then(
      (bitmap) => {
        const final = state.status === "off" || state.status === "closed";
        if (stopped || final || seq < shown || mine !== gen) {
          bitmap.close();
          return;
        }
        shown = seq;
        state.frame?.close();
        publish({
          frame: bitmap,
          size:
            bitmap.width > 0 && bitmap.height > 0 && bitmap.width <= 4096 && bitmap.height <= 4096
              ? { w: bitmap.width, h: bitmap.height }
              : w > 0 && ht > 0
                ? { w, h: ht }
                : null,
          status: "live",
          masked: null,
        });
      },
      () => {
        /* a frame that does not decode is skipped; the next one repaints */
      },
    );
  };

  const onStatus = (h: Header) => {
    const patch: Partial<ScreenSnapshot> = {};
    if (typeof h.busy === "boolean") patch.busy = h.busy;
    if (typeof h.origin === "string") patch.origin = h.origin;
    if (typeof h.unreachable === "string") patch.unreachable = h.unreachable;
    if (h.upstream === "asleep" || h.upstream === "no_screen")
      Object.assign(patch, { status: h.upstream, busy: false }, dropFrame());
    else if (h.upstream === "starting" || h.chromium === "stopped") {
      // already "stopped": still waiting for the agent's next step, not starting again
      if (state.status !== "stopped") patch.status = "starting";
    } else if (h.upstream === "live" || h.chromium === "running") patch.status = "live";
    publish(patch);
  };

  const onMessage = (data: unknown) => {
    if (!(data instanceof ArrayBuffer)) return; // the screen speaks binary only
    let msg: ReturnType<typeof decode>;
    try {
      msg = decode(data);
    } catch {
      return; // a malformed message is skipped, never drawn
    }
    const h = msg.header;
    // Only an attached relay sends these, so the pipe works and the next drop starts the backoff
    // over. A refusal says hello and bye too: counting those would reconnect every 500 ms.
    if (h.t === "frame" || h.t === "status" || h.t === "masked") backoff = BACKOFF_MS.first;
    if (h.t === "frame") draw(msg.payload, h);
    else if (h.t === "status") onStatus(h);
    else if (h.t === "masked") {
      const who = maskOf(h);
      if (who !== null) gen++; // frames already decoding were taken before the mask
      publish({ masked: who, ...(who !== null ? dropCursor() : {}) });
    } else if (h.t === "role") publish({ driver: h.driver === true });
    else if (h.t === "reset") {
      // a new incarnation (a restarted Chromium, another tab): nothing of the old one is shown
      // again; its frames follow, and its address comes with its next status
      gen++;
      state.frame?.close();
      publish({ frame: null, cursor: null });
    } else if (h.t === "cursor") onCursor(h, msg.payload);
    else if (h.t === "vnc") onVnc(h, msg.payload);
    // hello and bye need nothing here: a bye is followed by its close
  };

  const open = async () => {
    const mine = ++attempt;
    let ticket: string;
    try {
      ticket = await mintTicket(opts.taskId, opts.conn);
    } catch (e) {
      if (stopped || mine !== attempt) return;
      if (e instanceof ApiError && e.status === 409)
        return publish({ status: "off", ...dropFrame() });
      if (e instanceof ApiError && [401, 403, 404].includes(e.status))
        return publish({ status: "closed", ...dropFrame() });
      return reconnect();
    }
    if (stopped || mine !== attempt) return;
    const ws = new WebSocket(screenUrl(opts.taskId, ticket, opts.pane));
    ws.binaryType = "arraybuffer";
    socket = ws;
    ws.onopen = () => {
      if (socket !== ws) return;
      sendNow({ t: "hello", v: 1 });
      sendNow({
        t: "hint",
        fps: opts.pane ? SCREEN_FPS.pane : SCREEN_FPS.thumb,
        paused: tabHidden(),
      });
    };
    ws.onmessage = (ev: MessageEvent) => {
      if (socket === ws) onMessage(ev.data);
    };
    ws.onclose = (ev: CloseEvent) => {
      if (socket !== ws) return;
      socket = null;
      if (state.vnc) publish(endVnc()); // a dropped socket ends the stream
      // final: the last frame goes with it, never left under "Computer use is off." (UI check D4)
      if (ev.code === CLOSE_CODE.off)
        return publish({ status: "off", busy: false, ...dropFrame() });
      if (ev.code === CLOSE_CODE.notFound)
        return publish({ status: "closed", busy: false, ...dropFrame() });
      reconnect();
    };
  };

  const recheck = () => {
    if (stopped || state.status !== "off") return;
    backoff = BACKOFF_MS.first;
    publish({ status: "connecting" });
    void open();
  };

  return {
    start() {
      if (!stopped) return;
      stopped = false;
      running.add(recheck);
      backoff = BACKOFF_MS.first;
      if (typeof document !== "undefined")
        document.addEventListener("visibilitychange", onVisibility);
      publish({ status: "connecting" });
      void open();
    },
    stop() {
      if (stopped) return;
      stopped = true;
      running.delete(recheck);
      attempt++;
      if (retry) clearTimeout(retry);
      retry = null;
      if (stalling) clearTimeout(stalling);
      stalling = null;
      if (typeof document !== "undefined")
        document.removeEventListener("visibilitychange", onVisibility);
      const ws = socket;
      socket = null;
      ws?.close(1000);
      state.frame?.close();
      state.vnc?.end();
      if (shapeUrl) URL.revokeObjectURL(shapeUrl);
      shapeUrl = null;
      state = IDLE_SCREEN;
    },
    send: sendNow,
  };
}

/** The lease as the page should draw it: the reducer's `screen` (seeded from the task read's
 *  `machine.control`, then every `screen.state`, then reseeded from the task read after each).
 *  The web never expires a lease on its own clock: a lease ends for the client only when the
 *  server says so. `_now` is kept for the signature and ignored. */
export function currentLease(lease: ScreenLease | null, _now?: number): ScreenLease | null {
  return lease;
}

export function useScreen(
  taskId: string,
  opts: { pane: boolean; enabled: boolean },
): ScreenSnapshot & { conn: string; send: (h: Header, payload?: Uint8Array) => void } {
  // The pane's connection is kept per task in this tab (`paneConn`): it claims control with ITS
  // conn, and a reopened pane is the same one. The thumbnail mints its own, so the rail's
  // thumbnail (a second instance) is never the holder's socket. The pane's is this tab's alone
  // (`holdPaneConn`): it connects once the lock is its own, and a copy mints a fresh one.
  const [conn, setConn] = useState(() => (opts.pane ? paneConn(taskId) : mintConn()));
  const [owned, setOwned] = useState(!opts.pane);
  const [snap, setSnap] = useState<ScreenSnapshot>(IDLE_SCREEN);
  const client = useRef<ScreenClient | null>(null);
  const { pane, enabled } = opts;
  useEffect(() => {
    if (!pane) return;
    setOwned(false);
    return holdPaneConn(
      conn,
      () => setOwned(true),
      () => setConn(freshPaneConn(taskId)),
    );
  }, [pane, conn, taskId]);
  useEffect(() => {
    if (!enabled || !owned) {
      setSnap(IDLE_SCREEN);
      return;
    }
    const c = createScreenClient({ taskId, conn, pane, onState: setSnap });
    client.current = c;
    c.start();
    return () => {
      c.stop();
      if (client.current === c) client.current = null;
      setSnap(IDLE_SCREEN); // the bitmap it held is closed: never draw it again
    };
  }, [taskId, conn, pane, enabled, owned]);
  const send = useCallback(
    (h: Header, payload?: Uint8Array) => client.current?.send(h, payload),
    [],
  );
  return { ...snap, conn, send };
}
