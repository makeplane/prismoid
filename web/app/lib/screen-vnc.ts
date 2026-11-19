/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The lease holder's desktop, on a driver whose door serves a control stream (VNC): noVNC's RFB
// attaches to a raw channel (its Websock.attach, the WebSocket-like object @novnc/novnc accepts in place of
// a URL) that the screen socket feeds - the relay's `vnc` messages down, the holder's bytes up in
// `vnc` messages. One channel per RFB stream: the relay's `vnc{state: "open"}` starts one, its
// `vnc{state: "closed"}` (or the socket dropping) ends it, and noVNC never sees two streams.
// Pure of React and the DOM; the pane mounts noVNC on it (`mountVnc`, routes/tasks.$id/screen-vnc).

/** The largest piece sent up in one message: well inside the wire's 4 MiB. */
export const VNC_PIECE = 1024 * 1024;

export type VncCloseEvent = { code: number; reason: string; wasClean: boolean };

export type VncChannel = {
  binaryType: string;
  protocol: string;
  readonly readyState: "open" | "closed";
  onopen: ((e: unknown) => void) | null;
  onerror: ((e: unknown) => void) | null;
  onclose: ((e: VncCloseEvent) => void) | null;
  onmessage: ((e: { data: ArrayBuffer }) => void) | null;
  send(data: ArrayBuffer | ArrayBufferView): void;
  close(): void;
  /** The relay's bytes for noVNC; held until it attaches. */
  push(bytes: Uint8Array): void;
  /** The relay ended this stream. */
  end(): void;
};

export function createVncChannel(sendUp: (payload: Uint8Array) => void): VncChannel {
  let state: "open" | "closed" = "open";
  let handler: ((e: { data: ArrayBuffer }) => void) | null = null;
  const held: ArrayBuffer[] = [];
  const copy = (b: Uint8Array): ArrayBuffer => b.slice().buffer as ArrayBuffer;
  const flush = () => {
    while (handler && held.length && state === "open") handler({ data: held.shift()! });
  };
  const channel: VncChannel = {
    binaryType: "arraybuffer",
    protocol: "",
    get readyState() {
      return state;
    },
    onopen: null,
    onerror: null,
    onclose: null,
    get onmessage() {
      return handler;
    },
    set onmessage(fn) {
      handler = fn;
      // noVNC sets this as it attaches: what came before goes to it after the attach returns
      queueMicrotask(flush);
    },
    send(data) {
      if (state !== "open") return;
      const all =
        data instanceof ArrayBuffer
          ? new Uint8Array(data)
          : new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
      for (let i = 0; i < all.byteLength; i += VNC_PIECE) sendUp(all.slice(i, i + VNC_PIECE));
    },
    close() {
      // noVNC's disconnect: it waits for onclose (a WebSocket's close event comes after close()
      // returns), so it is queued, once, and only for a stream that was still open
      if (state === "closed") return;
      state = "closed";
      held.length = 0;
      queueMicrotask(() => channel.onclose?.({ code: 1000, reason: "", wasClean: true }));
    },
    push(bytes) {
      if (state !== "open") return;
      held.push(copy(bytes));
      flush();
    },
    end() {
      if (state === "closed") return;
      state = "closed";
      held.length = 0;
      channel.onclose?.({ code: 1000, reason: "", wasClean: true });
    },
  };
  return channel;
}

export type RfbLike = {
  scaleViewport: boolean;
  resizeSession: boolean;
  focusOnClick: boolean;
  viewOnly: boolean;
  focus(): void;
  disconnect(): void;
};
export type RfbCtor = new (
  target: HTMLElement,
  channel: unknown,
  options?: { shared?: boolean },
) => RfbLike;

/** noVNC, loaded only when a holder's pane first needs it (its own chunk). */
export async function loadRfb(): Promise<RfbCtor> {
  return (await import("@novnc/novnc")).default as unknown as RfbCtor;
}

/** Mount noVNC's RFB in `target` on `channel` once it loads: scaled into the pane, never resizing
 *  the remote desktop, not shared (one controller). The returned unmount disconnects it, and an
 *  unmount before the load finished means it is never made (React's strict-mode double effect);
 *  a channel the relay already ended is never mounted, nor disconnected again on unmount. A load
 *  that fails mounts nothing. */
export function mountVnc(
  target: HTMLElement,
  channel: VncChannel,
  load: () => Promise<RfbCtor>,
): () => void {
  let gone = false;
  let rfb: RfbLike | null = null;
  load().then(
    (RFB) => {
      if (gone || channel.readyState !== "open") return;
      rfb = new RFB(target, channel, { shared: false });
      rfb.scaleViewport = true;
      rfb.resizeSession = false;
      rfb.focusOnClick = true;
      rfb.viewOnly = false;
      rfb.focus();
    },
    () => {
      /* noVNC did not load: the pane keeps no desktop, and Hand back still works */
    },
  );
  return () => {
    gone = true;
    // a closed stream already disconnected noVNC (its onclose): a second disconnect is an error
    if (channel.readyState === "open") rfb?.disconnect();
    rfb = null;
  };
}
