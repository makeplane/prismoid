/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The screen's one drawing surface: the latest frame painted, and nothing at all once there is none
// (a cleared canvas, never the last page under "Asleep" or "Computer use is off.").
import { describe, expect, it, vi } from "vitest";
import { paint } from "./screen-canvas";

function canvas() {
  const ctx = { drawImage: vi.fn(), clearRect: vi.fn() };
  return {
    ctx,
    el: { width: 1280, height: 900, getContext: () => ctx } as unknown as HTMLCanvasElement,
  };
}

describe("paint", () => {
  it("draws a frame over the whole canvas, and clears it when the frame goes", () => {
    const { ctx, el } = canvas();
    const frame = {} as ImageBitmap;
    paint(el, frame);
    expect(ctx.drawImage).toHaveBeenCalledWith(frame, 0, 0, 1280, 900);
    paint(el, null);
    expect(ctx.clearRect).toHaveBeenCalledWith(0, 0, 1280, 900);
  });

  it("a bitmap closed under it is skipped, never thrown", () => {
    const { ctx, el } = canvas();
    ctx.drawImage.mockImplementation(() => {
      throw new Error("closed");
    });
    expect(() => paint(el, {} as ImageBitmap)).not.toThrow();
  });
});
