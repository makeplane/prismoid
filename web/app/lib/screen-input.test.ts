/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// Input to the remote browser: pointer events land on the frame's own pixels, keys
// carry what CDP needs to type them, a paste becomes bounded `text`, and Esc twice gives the
// keyboard back. Pure functions over fake events.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  createClickCounter,
  createHeldInput,
  createInputHold,
  createReleaseChord,
  MASK_SETTLE_MS,
  keyHeader,
  modifiers,
  pasteChunks,
  PASTE_MAX,
  pointerHeader,
} from "./screen-input";
import { encode } from "./screen-wire";

const FRAME = { w: 1280, h: 900 };
// the canvas drawn at half size, 100 px from the left and 50 px down
const RECT = { left: 100, top: 50, width: 640, height: 450 } as DOMRect;

const pointer = (over: Record<string, unknown> = {}) =>
  ({
    clientX: 420,
    clientY: 275,
    button: 0,
    buttons: 0,
    detail: 1,
    altKey: false,
    ctrlKey: false,
    metaKey: false,
    shiftKey: false,
    ...over,
  }) as unknown as PointerEvent;

const key = (over: Record<string, unknown> = {}) =>
  ({
    key: "a",
    code: "KeyA",
    keyCode: 65,
    isComposing: false,
    repeat: false,
    timeStamp: 0,
    altKey: false,
    ctrlKey: false,
    metaKey: false,
    shiftKey: false,
    ...over,
  }) as unknown as KeyboardEvent;

describe("pointerHeader", () => {
  it("maps the canvas rect onto the frame's pixels", () => {
    expect(pointerHeader(pointer(), RECT, FRAME, "move")).toEqual({
      t: "pointer",
      type: "move",
      x: 640,
      y: 450,
      button: "none",
      buttons: 0,
      clicks: 0,
      modifiers: 0,
    });
  });

  it("clamps a point outside the canvas (a drag that left it) to the frame", () => {
    const h = pointerHeader(
      pointer({ clientX: 0, clientY: 9999, buttons: 1 }),
      RECT,
      FRAME,
      "move",
    );
    expect([h.x, h.y, h.buttons]).toEqual([0, 899, 1]);
  });

  it("names the button and counts clicks on press and release", () => {
    expect(pointerHeader(pointer({ button: 2, buttons: 2 }), RECT, FRAME, "down")).toMatchObject({
      type: "down",
      button: "right",
      buttons: 2,
      clicks: 1,
    });
    expect(pointerHeader(pointer({ button: 0, detail: 2 }), RECT, FRAME, "up")).toMatchObject({
      button: "left",
      clicks: 2,
    });
    expect(pointerHeader(pointer({ button: 1 }), RECT, FRAME, "down").button).toBe("middle");
  });

  it("converts wheel deltas to pixels: lines are 16 px, pages a frame tall", () => {
    const wheel = (deltaMode: number) =>
      pointerHeader(
        pointer({ deltaX: 0, deltaY: 3, deltaMode }) as unknown as WheelEvent,
        RECT,
        FRAME,
        "wheel",
      );
    expect(wheel(0)).toMatchObject({ type: "wheel", dx: 0, dy: 3 });
    expect(wheel(1).dy).toBe(48);
    expect(wheel(2).dy).toBe(2700);
  });
});

describe("modifiers - CDP's bitmask", () => {
  it("Alt=1, Ctrl=2, Meta=4, Shift=8", () => {
    const m = (o: Record<string, boolean>) =>
      modifiers({ altKey: false, ctrlKey: false, metaKey: false, shiftKey: false, ...o });
    expect([
      m({ altKey: true }),
      m({ ctrlKey: true }),
      m({ metaKey: true }),
      m({ shiftKey: true }),
    ]).toEqual([1, 2, 4, 8]);
    expect(m({ altKey: true, ctrlKey: true, metaKey: true, shiftKey: true })).toBe(15);
  });
});

describe("keyHeader", () => {
  it("a printable key-down carries its text; the key-up does not", () => {
    expect(keyHeader(key(), "down")).toEqual({
      t: "key",
      type: "down",
      key: "a",
      code: "KeyA",
      modifiers: 0,
      keyCode: 65,
      text: "a",
    });
    expect(keyHeader(key(), "up")).not.toHaveProperty("text");
  });

  it("Enter types a carriage return; a shortcut types nothing", () => {
    expect(keyHeader(key({ key: "Enter", code: "Enter", keyCode: 13 }), "down")?.text).toBe("\r");
    const selectAll = keyHeader(key({ ctrlKey: true }), "down");
    expect(selectAll).toMatchObject({ key: "a", modifiers: 2 });
    expect(selectAll).not.toHaveProperty("text");
    expect(
      keyHeader(key({ key: "Backspace", code: "Backspace", keyCode: 8 }), "down"),
    ).not.toHaveProperty("text");
  });

  it("drops IME composition and dead keys, and leaves the paste shortcut to the paste event", () => {
    expect(keyHeader(key({ isComposing: true }), "down")).toBeNull();
    expect(keyHeader(key({ key: "Process" }), "down")).toBeNull();
    expect(keyHeader(key({ key: "Dead" }), "down")).toBeNull();
    expect(keyHeader(key({ key: "v", metaKey: true }), "down")).toBeNull();
    expect(keyHeader(key({ key: "V", ctrlKey: true, shiftKey: true }), "down")).toBeNull();
  });
});

describe("pasteChunks", () => {
  it("splits a paste into messages of at most 1,000 characters", () => {
    expect(pasteChunks("")).toEqual([]);
    expect(pasteChunks("x".repeat(2500)).map((c) => c.length)).toEqual([1000, 1000, 500]);
    expect(PASTE_MAX).toBe(1000);
  });

  it("never splits a surrogate pair", () => {
    const chunks = pasteChunks("😀".repeat(1001));
    expect(chunks.map((c) => [...c].length)).toEqual([1000, 1]);
    expect(chunks.join("")).toBe("😀".repeat(1001));
  });

  it("keeps every message's header under the wire's limit, however the text escapes", () => {
    const controls = "\u0001".repeat(2500); // six bytes apiece once JSON-escaped
    const chunks = pasteChunks(controls);
    expect(chunks.join("")).toBe(controls);
    for (const text of chunks) expect(() => encode({ t: "text", text })).not.toThrow();
  });

  it("sends a lone surrogate as the replacement character, never as half a pair", () => {
    expect(pasteChunks("a\ud800b\udc00")).toEqual(["a�b�"]);
    expect(pasteChunks("😀")).toEqual(["😀"]);
  });
});

describe("the Esc-Esc release chord", () => {
  const esc = (timeStamp: number, repeat = false) =>
    key({ key: "Escape", code: "Escape", timeStamp, repeat });

  it("fires on the second Esc within 600 ms, and only then", () => {
    const chord = createReleaseChord();
    expect(chord(esc(0))).toBe(false);
    expect(chord(esc(500))).toBe(true);
    expect(chord(esc(700))).toBe(false); // a fired chord starts over
  });

  it("a slow second Esc starts a new window", () => {
    const chord = createReleaseChord();
    expect(chord(esc(0))).toBe(false);
    expect(chord(esc(700))).toBe(false);
    expect(chord(esc(900))).toBe(true);
  });

  it("another key in between, or auto-repeat, never counts", () => {
    const chord = createReleaseChord();
    chord(esc(0));
    chord(key({ timeStamp: 100 }));
    expect(chord(esc(200))).toBe(false);
    expect(chord(esc(250, true))).toBe(false);
  });
});

describe("the pane's own click count", () => {
  it("a header takes the pane's count over PointerEvent.detail", () => {
    const h = pointerHeader(pointer({ detail: 0 }), RECT, FRAME, "down", 3);
    expect(h.clicks).toBe(3);
    expect(pointerHeader(pointer({ detail: 0 }), RECT, FRAME, "down").clicks).toBe(1);
  });

  it("counts presses close in time and place, and starts over otherwise", () => {
    const c = createClickCounter();
    const down = (timeStamp: number, clientX = 10, button = 0) =>
      c.down({ timeStamp, clientX, clientY: 10, button });
    expect(down(0)).toBe(1);
    expect(down(200)).toBe(2);
    expect(c.current()).toBe(2);
    expect(down(350)).toBe(3);
    expect(down(2000)).toBe(1); // too slow
    expect(down(2100, 80)).toBe(1); // too far
    expect(down(2200, 80, 2)).toBe(1); // another button
  });
});

describe("the private input hold", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());
  const ADA = "ada";
  const lease = (priv: boolean, holderId: string | null = ADA) => ({
    holderId,
    me: ADA,
    private: priv,
  });

  it("an open claim may type at once; someone else's lease never may", () => {
    const hold = createInputHold(() => {});
    hold.sync(lease(false));
    expect(hold.ready()).toBe(true);
    hold.sync(lease(false, "bob"));
    expect(hold.ready()).toBe(false);
  });

  it("after a Private claim: no input until the lease is seen private AND the settle has passed", () => {
    const seen: boolean[] = [];
    const hold = createInputHold((r) => seen.push(r));
    hold.expectPrivate();
    hold.sync(lease(false)); // the claim answered, the lease not yet private
    vi.advanceTimersByTime(5000);
    expect(hold.ready()).toBe(false); // never seen private: still nothing
    hold.sync(lease(true)); // screen.state says private
    expect(hold.ready()).toBe(false);
    vi.advanceTimersByTime(MASK_SETTLE_MS - 1);
    expect(hold.ready()).toBe(false);
    vi.advanceTimersByTime(1);
    expect(hold.ready()).toBe(true);
    expect(seen.at(-1)).toBe(true);
  });

  it("a switch to Private from an open lease holds the same way", () => {
    const hold = createInputHold(() => {});
    hold.sync(lease(false));
    expect(hold.ready()).toBe(true);
    hold.expectPrivate();
    expect(hold.ready()).toBe(false);
    hold.sync(lease(true));
    vi.advanceTimersByTime(MASK_SETTLE_MS);
    expect(hold.ready()).toBe(true);
  });

  it("losing the lease clears the hold", () => {
    const hold = createInputHold(() => {});
    hold.sync(lease(true));
    vi.advanceTimersByTime(MASK_SETTLE_MS);
    expect(hold.ready()).toBe(true);
    hold.sync({ holderId: null, me: ADA, private: false });
    expect(hold.ready()).toBe(false);
  });
});

describe("held input is released when driving ends", () => {
  it("sends a key-up for every held key and a pointer-up for a held button", () => {
    const held = createHeldInput();
    held.key(keyHeader(key({ key: "Shift", code: "ShiftLeft", keyCode: 16 }), "down")!);
    held.key(keyHeader(key(), "down")!);
    held.key(keyHeader(key(), "up")!); // a released key is not held
    held.pointer(pointerHeader(pointer({ button: 0, buttons: 1 }), RECT, FRAME, "down"));
    const out = held.release();
    expect(out).toHaveLength(2);
    expect(out[0]).toMatchObject({ t: "key", type: "up", code: "ShiftLeft" });
    expect(out[0]).not.toHaveProperty("text");
    expect(out[1]).toMatchObject({
      t: "pointer",
      type: "up",
      button: "left",
      buttons: 0,
      x: 640,
      y: 450,
    });
    expect(held.release()).toEqual([]); // once
  });

  it("a button already released leaves nothing to undo", () => {
    const held = createHeldInput();
    held.pointer(pointerHeader(pointer({ buttons: 1 }), RECT, FRAME, "down"));
    held.pointer(pointerHeader(pointer({ buttons: 0 }), RECT, FRAME, "up"));
    expect(held.release()).toEqual([]);
  });
});
