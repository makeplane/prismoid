/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import {
  type ClipboardEvent as ReactClipboardEvent,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type RefObject,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  createClickCounter,
  createHeldInput,
  createInputHold,
  createReleaseChord,
  keyHeader,
  pasteChunks,
  pointerHeader,
} from "~/lib/screen-input";
import type { VncChannel } from "~/lib/screen-vnc";
import type { Header } from "~/lib/screen-wire";

/** The pane shows the desktop's VNC instead of the frames: this tab holds control and the relay
 *  streams VNC to it (a driver whose door serves a control stream). Docker and sandbar never
 *  stream it. Pure. */
export function vncModeOf(p: { holding: boolean; vnc: VncChannel | null }): boolean {
  return p.holding && p.vnc !== null;
}

type Send = (h: Header) => void;

/** The canvas's input handlers while driving, else only its tab stop. */
function canvasInputProps(p: {
  driving: boolean;
  holding: boolean;
  onPointer: (e: ReactPointerEvent<HTMLCanvasElement>, kind: "down" | "up" | "move") => void;
  onKey: (e: ReactKeyboardEvent<HTMLCanvasElement>, kind: "down" | "up") => void;
  releaseHeld: () => void;
  send: Send;
}) {
  if (!p.driving) return { tabIndex: p.holding ? 0 : -1 };
  return {
    tabIndex: 0,
    onPointerDown: (e: ReactPointerEvent<HTMLCanvasElement>) => p.onPointer(e, "down"),
    onPointerUp: (e: ReactPointerEvent<HTMLCanvasElement>) => p.onPointer(e, "up"),
    onPointerMove: (e: ReactPointerEvent<HTMLCanvasElement>) => p.onPointer(e, "move"),
    onBlur: p.releaseHeld,
    onContextMenu: (e: { preventDefault: () => void }) => e.preventDefault(),
    onKeyDown: (e: ReactKeyboardEvent<HTMLCanvasElement>) => p.onKey(e, "down"),
    onKeyUp: (e: ReactKeyboardEvent<HTMLCanvasElement>) => p.onKey(e, "up"),
    onPaste: (e: ReactClipboardEvent<HTMLCanvasElement>) => {
      e.preventDefault();
      for (const text of pasteChunks(e.clipboardData.getData("text/plain")))
        p.send({ t: "text", text });
    },
  };
}

/**
 * The screen's input, only while this tab holds control and only where driving is offered at
 * all (`holding`). A private claim holds input until the mask is seen and settled
 * (createInputHold); pointer moves are coalesced to one per animation frame; whatever the remote
 * page is holding (a key, a button) is undone when driving ends. The decisions live pure in
 * `lib/screen-input.ts`; this hook only wires them to the canvas.
 */
export function useScreenInput({
  canvasRef,
  send,
  size,
  vnc,
  holding,
  holderId,
  me,
  leasePrivate,
}: {
  canvasRef: RefObject<HTMLCanvasElement | null>;
  send: Send;
  size: { w: number; h: number } | null;
  vnc: VncChannel | null;
  holding: boolean;
  holderId: string | null;
  me: string;
  leasePrivate: boolean;
}) {
  const [inputReady, setInputReady] = useState(false);
  const holdRef = useRef<ReturnType<typeof createInputHold> | null>(null);
  if (!holdRef.current) holdRef.current = createInputHold(setInputReady);
  const hold = holdRef.current;
  useEffect(() => {
    hold.sync({ holderId, me, private: leasePrivate });
  }, [hold, holderId, me, leasePrivate]);
  useEffect(() => () => hold.dispose(), [hold]);
  const driving = holding && inputReady && hold.ready();
  const vncMode = vncModeOf({ holding, vnc });
  // The holder's own frames are not needed while the desktop streams VNC: paused, and resumed
  // (as the tab's visibility says) when control ends.
  useEffect(() => {
    if (!vncMode) return;
    send({ t: "hint", paused: true });
    return () =>
      send({
        t: "hint",
        paused: typeof document !== "undefined" && document.visibilityState === "hidden",
      });
  }, [vncMode, send]);
  const drivingRef = useRef(driving);
  drivingRef.current = driving;
  const held = useRef(createHeldInput()).current;
  const clicks = useRef(createClickCounter()).current;
  const chord = useMemo(() => createReleaseChord(), []);
  const sizeRef = useRef(size);
  sizeRef.current = size;
  const pendingMove = useRef<Header | null>(null);
  const raf = useRef(0);
  const flushMove = useCallback(() => {
    if (raf.current) cancelAnimationFrame(raf.current);
    raf.current = 0;
    // a move queued before driving ended is dropped, never sent late
    if (pendingMove.current && drivingRef.current) send(pendingMove.current);
    pendingMove.current = null;
  }, [send]);
  // Driving ends (blur, a lost lease, Esc-Esc, unmount, mid-drag): drop the queued move and undo
  // whatever the remote page is holding, so it is never left with a key or a button down.
  const releaseHeld = useCallback(() => {
    if (raf.current) cancelAnimationFrame(raf.current);
    raf.current = 0;
    pendingMove.current = null;
    for (const h of held.release()) send(h);
  }, [held, send]);
  useEffect(() => {
    if (!driving) return;
    return releaseHeld;
  }, [driving, releaseHeld]);
  // The canvas unmounts while the VNC view stands in for it and a new element mounts after: the
  // focus and the wheel listener re-bind with vncMode.
  useEffect(() => {
    if (holding) canvasRef.current?.focus();
  }, [holding, vncMode, canvasRef]);
  useEffect(() => {
    const el = canvasRef.current;
    if (!el || !driving) return;
    // native and non-passive: React's onWheel cannot stop the page from scrolling
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      if (sizeRef.current)
        send(pointerHeader(e, el.getBoundingClientRect(), sizeRef.current, "wheel"));
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [driving, send, vncMode, canvasRef]);
  const onPointer = (e: ReactPointerEvent<HTMLCanvasElement>, kind: "down" | "up" | "move") => {
    const el = canvasRef.current;
    if (!el || !size) return;
    // the pane's own count: PointerEvent.detail is 0 for pointer events in several browsers
    const count = kind === "down" ? clicks.down(e.nativeEvent) : clicks.current();
    const h = pointerHeader(e.nativeEvent, el.getBoundingClientRect(), size, kind, count);
    if (kind === "move") {
      // moves are coalesced to one per animation frame; the relay coalesces again
      pendingMove.current = h;
      if (!raf.current) raf.current = requestAnimationFrame(flushMove);
      return;
    }
    flushMove();
    held.pointer(h);
    if (kind === "down") {
      e.preventDefault();
      el.focus();
      el.setPointerCapture(e.pointerId);
    } else if (el.hasPointerCapture(e.pointerId)) {
      el.releasePointerCapture(e.pointerId);
    }
    send(h);
  };
  const onKey = (e: ReactKeyboardEvent<HTMLCanvasElement>, kind: "down" | "up") => {
    if (kind === "down" && chord(e.nativeEvent)) {
      e.preventDefault();
      releaseHeld();
      canvasRef.current?.blur();
      return;
    }
    const h = keyHeader(e.nativeEvent, kind);
    if (!h) return; // the paste shortcut falls through to the paste event
    // The keyboard is the remote page's while you drive: no page shortcut (the sidebar's
    // Cmd+B, a palette) sees the key on its way to window.
    e.preventDefault();
    e.stopPropagation();
    held.key(h);
    send(h);
  };
  const inputProps = canvasInputProps({ driving, holding, onPointer, onKey, releaseHeld, send });
  return { hold, driving, vncMode, inputProps };
}
