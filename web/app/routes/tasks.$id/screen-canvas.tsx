/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useEffect, useRef, type ComponentProps, type RefObject } from "react";

/** The screen's one drawing surface (thumbnail and pane): the latest decoded frame, drawn at
 *  the frame's own size. CSS scales the element, never the bitmap, so the element's box IS the
 *  drawn area - the pane's pointer mapping relies on that. No colour is ever painted here: the
 *  frame is the remote page's pixels, and every overlay is a token-styled element above it. */
/** Paint the latest frame over the whole canvas, or clear it when there is none: a canvas keeps
 *  its pixels until told, and React never touches an unchanged one. A bitmap closed under us (a
 *  newer frame replaced it) is skipped: the next frame repaints. */
export function paint(el: HTMLCanvasElement, frame: ImageBitmap | null): void {
  try {
    const ctx = el.getContext("2d");
    if (frame) ctx?.drawImage(frame, 0, 0, el.width, el.height);
    else ctx?.clearRect(0, 0, el.width, el.height);
  } catch {
    /* a closed bitmap */
  }
}

export function ScreenCanvas({
  frame,
  size,
  canvasRef,
  ...props
}: {
  frame: ImageBitmap | null;
  size: { w: number; h: number } | null;
  /** The pane's handle on the element (focus, the pointer rect); the thumbnail needs none. */
  canvasRef?: RefObject<HTMLCanvasElement | null>;
} & Omit<ComponentProps<"canvas">, "ref" | "width" | "height">) {
  const own = useRef<HTMLCanvasElement>(null);
  const ref = canvasRef ?? own;
  useEffect(() => {
    if (ref.current) paint(ref.current, frame);
  }, [frame, ref]);
  // 1280x900 before the first frame: the sidecar's native viewport, so the box keeps its shape.
  return <canvas ref={ref} width={size?.w ?? 1280} height={size?.h ?? 900} {...props} />;
}
