/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The screen's input, as the wire's up-messages: pure functions from DOM
// events to `pointer`, `key` and `text` headers. The sidecar dispatches them through CDP
// (`Input.dispatchMouseEvent`, `Input.dispatchKeyEvent`, `Input.insertText`), so the fields
// follow CDP: coordinates in the frame's CSS pixels, CDP button names, DOM's `buttons` bitmask
// (the same bits CDP takes), and CDP's modifier bitmask. Nothing here touches a socket.

import { encode, HEADER_MAX, type Header } from "./screen-wire";

/** A pasted `text` message carries at most this many characters; longer pastes are split. */
export const PASTE_MAX = 1000;
/** Esc twice inside this window gives the keyboard back to the page. */
export const RELEASE_CHORD_MS = 600;

type Mods = Pick<KeyboardEvent, "altKey" | "ctrlKey" | "metaKey" | "shiftKey">;

/** CDP's modifier bitmask: Alt=1, Ctrl=2, Meta/Command=4, Shift=8. */
export function modifiers(ev: Mods): number {
  return (ev.altKey ? 1 : 0) | (ev.ctrlKey ? 2 : 0) | (ev.metaKey ? 4 : 0) | (ev.shiftKey ? 8 : 0);
}

// DOM MouseEvent.button (0 main, 1 auxiliary, 2 secondary, 3 back, 4 forward) -> CDP's names.
const BUTTONS = ["left", "middle", "right", "back", "forward"] as const;
// WheelEvent.deltaMode: 0 pixels, 1 lines, 2 pages. CDP wants pixels.
const LINE_PX = 16;

const clamp = (v: number, hi: number) => Math.min(Math.max(v, 0), hi);
const tenth = (v: number) => Math.round(v * 10) / 10;

/** A pointer event over the canvas, in the frame's coordinates. `rect` is the canvas's client
 *  rect, which is exactly the drawn frame (the canvas is CSS-scaled, never letterboxed). */
export function pointerHeader(
  ev: PointerEvent | WheelEvent,
  rect: DOMRect,
  frame: { w: number; h: number },
  kind: "move" | "down" | "up" | "wheel",
  /** The pane's own click count (`createClickCounter`); `PointerEvent.detail` is 0 for
   *  pointer events in several browsers, so it is only the fallback. */
  clicks?: number,
): Header {
  const x = tenth(clamp(((ev.clientX - rect.left) / rect.width) * frame.w, frame.w - 1));
  const y = tenth(clamp(((ev.clientY - rect.top) / rect.height) * frame.h, frame.h - 1));
  const header: Header = {
    t: "pointer",
    type: kind,
    x,
    y,
    button: kind === "down" || kind === "up" ? (BUTTONS[ev.button] ?? "none") : "none",
    buttons: ev.buttons,
    clicks: kind === "down" || kind === "up" ? Math.max(1, clicks ?? ev.detail) : 0,
    modifiers: modifiers(ev),
  };
  if (kind === "wheel") {
    const w = ev as WheelEvent;
    const unit = w.deltaMode === 1 ? LINE_PX : w.deltaMode === 2 ? frame.h : 1;
    header.dx = tenth(w.deltaX * unit);
    header.dy = tenth(w.deltaY * unit);
  }
  return header;
}

/** A key event for the remote page, or null for one that must not go: an IME composition or
 *  a dead key (paste is the hatch for those), and the paste shortcut itself, whose
 *  `paste` event carries the text instead. `text` rides only a plain key-down that types. */
export function keyHeader(ev: KeyboardEvent, kind: "down" | "up"): Header | null {
  if (ev.isComposing || ev.key === "Process" || ev.key === "Dead" || ev.key === "Unidentified")
    return null;
  const shortcut = ev.ctrlKey || ev.metaKey;
  if (shortcut && ev.key.toLowerCase() === "v") return null;
  const header: Header = {
    t: "key",
    type: kind,
    key: ev.key,
    code: ev.code,
    modifiers: modifiers(ev),
    keyCode: ev.keyCode || 0,
  };
  if (kind === "down" && !shortcut) {
    if (ev.key === "Enter") header.text = "\r";
    else if ([...ev.key].length === 1) header.text = ev.key;
  }
  return header;
}

// A `text` message's header without its text, as `encode` writes it: what each chunk's escaped
// text must leave room for under HEADER_MAX.
const TEXT_HEADER_BYTES = encode({ t: "text", text: "" }).byteLength - 4;
const LONE_SURROGATE = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/g;
const utf8 = new TextEncoder();

/** A paste, as `text` messages the wire can carry: at most PASTE_MAX characters each - counted in
 *  code points, so a surrogate pair is never split (the server counts the same way) - and each
 *  header under HEADER_MAX once JSON-escaped (a control character escapes to six bytes, so a
 *  thousand of them would not fit). A lone surrogate is no character: it goes as U+FFFD. */
export function pasteChunks(text: string): string[] {
  const out: string[] = [];
  let chunk: string[] = [];
  let bytes = TEXT_HEADER_BYTES;
  for (const ch of text.replace(LONE_SURROGATE, "\uFFFD")) {
    const size = utf8.encode(JSON.stringify(ch)).byteLength - 2; // the quotes are counted once
    if (chunk.length === PASTE_MAX || bytes + size > HEADER_MAX) {
      out.push(chunk.join(""));
      chunk = [];
      bytes = TEXT_HEADER_BYTES;
    }
    chunk.push(ch);
    bytes += size;
  }
  if (chunk.length) out.push(chunk.join(""));
  return out;
}

/** The pane's own click counter: a press within CLICK_MS and CLICK_SLOP_PX of the last one adds
 *  to the count (double, triple), else starts at 1. `down` returns the count; the matching `up`
 *  reads it back with `current()`. */
export const CLICK_MS = 500;
export const CLICK_SLOP_PX = 5;
export function createClickCounter(): {
  down(ev: { timeStamp: number; clientX: number; clientY: number; button: number }): number;
  current(): number;
} {
  let last: { t: number; x: number; y: number; button: number } | null = null;
  let count = 0;
  return {
    down(ev) {
      const again =
        last != null &&
        last.button === ev.button &&
        ev.timeStamp - last.t <= CLICK_MS &&
        Math.abs(ev.clientX - last.x) <= CLICK_SLOP_PX &&
        Math.abs(ev.clientY - last.y) <= CLICK_SLOP_PX;
      count = again ? count + 1 : 1;
      last = { t: ev.timeStamp, x: ev.clientX, y: ev.clientY, button: ev.button };
      return count;
    },
    current: () => Math.max(1, count),
  };
}

/** After a private claim (or a switch to Private) the pane sends no input until it has SEEN the
 *  lease turn private for this holder and this long has passed: the mask is the server's, and a
 *  keystroke typed before it lands would cross the open wire. */
export const MASK_SETTLE_MS = 300;

export type InputHold = {
  /** The lease as the page sees it now (call on every change). */
  sync(s: { holderId: string | null; me: string; private: boolean }): void;
  /** The pane just asked for a private claim (or a switch to Private). */
  expectPrivate(): void;
  /** That ask was refused: nothing is pending any more. */
  cancelPrivate(): void;
  /** May this pane send input right now? (Never says yes for someone else's lease.) */
  ready(): boolean;
  dispose(): void;
};

export function createInputHold(
  onChange: (ready: boolean) => void,
  settleMs: number = MASK_SETTLE_MS,
): InputHold {
  let mine = false;
  let holder: string | null = null;
  let priv = false;
  let want = false;
  let settled = false;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let last = false;
  const ready = () => mine && (priv ? settled : !want);
  const emit = () => {
    const r = ready();
    if (r !== last) {
      last = r;
      onChange(r);
    }
  };
  const clear = () => {
    if (timer) clearTimeout(timer);
    timer = null;
  };
  const arm = () => {
    clear();
    settled = false;
    timer = setTimeout(() => {
      timer = null;
      settled = true;
      want = false;
      emit();
    }, settleMs);
  };
  return {
    sync(s) {
      const nowMine = s.holderId != null && s.holderId === s.me;
      const wasMine = mine;
      const wasPriv = mine && priv;
      const sameHolder = holder === s.holderId;
      mine = nowMine;
      holder = s.holderId;
      priv = s.private;
      if (!mine) {
        // a pending claim survives not being the holder yet; losing the lease it won does not
        if (wasMine) want = false;
        settled = false;
        clear();
      } else if (priv) {
        if (!wasPriv || !sameHolder) arm();
      } else {
        settled = false;
        clear();
      }
      emit();
    },
    expectPrivate() {
      want = true;
      if (mine && priv) arm();
      emit();
    },
    cancelPrivate() {
      want = false;
      emit();
    },
    ready,
    dispose: clear,
  };
}

/** The release chord: true on the second Esc key-down within `windowMs` of the first. Any other
 *  key in between starts over; a held key's auto-repeat never counts. */
export function createReleaseChord(
  windowMs: number = RELEASE_CHORD_MS,
): (ev: Pick<KeyboardEvent, "key" | "timeStamp" | "repeat">) => boolean {
  let first = Number.NEGATIVE_INFINITY;
  return (ev) => {
    if (ev.key !== "Escape") {
      first = Number.NEGATIVE_INFINITY;
      return false;
    }
    if (ev.repeat) return false;
    if (ev.timeStamp - first <= windowMs) {
      first = Number.NEGATIVE_INFINITY;
      return true;
    }
    first = ev.timeStamp;
    return false;
  };
}

/** What the remote page is holding because of this pane: keys pressed and a button down. When
 *  driving ends (blur, a lost lease, Esc-Esc, unmount, a drag that left) `release()` returns the
 *  key-ups and the pointer-up that undo them, so the remote page is never left holding either. */
export function createHeldInput(): {
  key(h: Header): void;
  pointer(h: Header): void;
  release(): Header[];
} {
  const keys = new Map<string, Header>();
  let button: Header | null = null;
  return {
    key(h) {
      const id = String(h.code || h.key);
      if (h.type === "down") keys.set(id, h);
      else keys.delete(id);
    },
    pointer(h) {
      if (h.type === "down") button = h;
      else if (h.type === "up" && !(typeof h.buttons === "number" && h.buttons > 0)) button = null;
    },
    release() {
      const out: Header[] = [];
      for (const d of keys.values()) {
        const up: Header = { ...d, type: "up" };
        delete up.text;
        out.push(up);
      }
      if (button) out.push({ ...button, type: "up", buttons: 0, clicks: 1 });
      keys.clear();
      button = null;
      return out;
    },
  };
}
