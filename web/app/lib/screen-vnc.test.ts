/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The raw channel noVNC's RFB attaches to (Websock.attach, @novnc/novnc 1.7.0): the members it
// checks for, bytes pushed before it attaches delivered once it does, in order, its sends going
// up only while open, and the relay's end closing it.
import { describe, expect, it, vi } from "vitest";
import { createVncChannel, mountVnc, type RfbLike, type VncCloseEvent } from "./screen-vnc";

// noVNC's own list (core/websock.js rawChannelProps): attach throws on a missing one.
const RAW_CHANNEL_PROPS = [
  "send",
  "close",
  "binaryType",
  "onerror",
  "onmessage",
  "onopen",
  "protocol",
  "readyState",
];

const bytes = (s: string) => new TextEncoder().encode(s);
const text = (b: ArrayBuffer) => new TextDecoder().decode(b);

describe("createVncChannel", () => {
  it("has every member noVNC's attach checks for", () => {
    const ch = createVncChannel(() => {});
    const own = [...Object.keys(ch), ...Object.getOwnPropertyNames(Object.getPrototypeOf(ch))];
    for (const prop of RAW_CHANNEL_PROPS) expect(own).toContain(prop);
    expect(ch.readyState).toBe("open");
  });

  it("holds the relay's bytes until noVNC attaches, then delivers them in order", async () => {
    const ch = createVncChannel(() => {});
    ch.push(bytes("RFB 003.008\n"));
    ch.push(bytes("\x01\x01"));
    const got: string[] = [];
    ch.onmessage = (e) => got.push(text(e.data));
    await Promise.resolve();
    ch.push(bytes("later"));
    expect(got).toEqual(["RFB 003.008\n", "\x01\x01", "later"]);
  });

  it("sends noVNC's bytes up while open, and nothing after it closes", () => {
    const up = vi.fn();
    const ch = createVncChannel(up);
    ch.send(bytes("RFB 003.008\n"));
    ch.send(new Uint8Array([5, 0, 0, 16, 0, 16]).buffer);
    ch.close();
    ch.send(bytes("late"));
    expect(up.mock.calls.map(([p]) => Array.from(p as Uint8Array))).toEqual([
      Array.from(bytes("RFB 003.008\n")),
      [5, 0, 0, 16, 0, 16],
    ]);
    expect(ch.readyState).toBe("closed");
  });

  it("splits a send larger than one wire message", () => {
    const up = vi.fn();
    const ch = createVncChannel(up);
    ch.send(new Uint8Array(1024 * 1024 * 3));
    expect(up.mock.calls.length).toBe(3);
    expect(up.mock.calls.every(([p]) => (p as Uint8Array).byteLength <= 1024 * 1024)).toBe(true);
  });

  it("ends when the relay says the stream closed: onclose once, then nothing", () => {
    const ch = createVncChannel(() => {});
    const closes: number[] = [];
    ch.onclose = (e) => closes.push(e.code);
    const got: unknown[] = [];
    ch.onmessage = (e) => got.push(e);
    ch.end();
    ch.end();
    ch.push(bytes("after"));
    expect(closes).toEqual([1000]);
    expect(got).toEqual([]);
    expect(ch.readyState).toBe("closed");
  });

  it("answers noVNC's own close with onclose once, after it returns, so its disconnect finishes", async () => {
    const ch = createVncChannel(() => {});
    const closes: VncCloseEvent[] = [];
    ch.onclose = (e) => closes.push(e);
    ch.close();
    expect(closes).toEqual([]); // queued, as a WebSocket's close event is
    await Promise.resolve();
    expect(closes).toEqual([{ code: 1000, reason: "", wasClean: true }]);
    ch.close();
    ch.end();
    await Promise.resolve();
    expect(closes.length).toBe(1);
  });

  it("never answers a close of a stream the relay already ended", async () => {
    const ch = createVncChannel(() => {});
    const closes: number[] = [];
    ch.onclose = (e) => closes.push(e.code);
    ch.end();
    ch.close();
    await Promise.resolve();
    expect(closes).toEqual([1000]);
  });
});

class FakeRfb implements RfbLike {
  static made: FakeRfb[] = [];
  scaleViewport = false;
  resizeSession = true;
  focusOnClick = false;
  viewOnly = true;
  disconnected = false;
  constructor(
    public target: unknown,
    public channel: unknown,
    public options?: { shared?: boolean },
  ) {
    FakeRfb.made.push(this);
  }
  focus() {}
  disconnect() {
    this.disconnected = true;
  }
  addEventListener() {}
}

describe("mountVnc", () => {
  it("mounts noVNC on the channel once it loads, scaled and not shared, and disconnects on unmount", async () => {
    FakeRfb.made = [];
    const ch = createVncChannel(() => {});
    const target = {} as HTMLElement;
    const unmount = mountVnc(target, ch, async () => FakeRfb);
    await Promise.resolve();
    await Promise.resolve();
    const rfb = FakeRfb.made[0];
    expect(rfb.channel).toBe(ch);
    expect(rfb.options).toEqual({ shared: false });
    expect([rfb.scaleViewport, rfb.resizeSession, rfb.viewOnly]).toEqual([true, false, false]);
    unmount();
    expect(rfb.disconnected).toBe(true);
  });

  it("never mounts after an unmount that came first (a strict-mode double effect)", async () => {
    FakeRfb.made = [];
    const unmount = mountVnc(
      {} as HTMLElement,
      createVncChannel(() => {}),
      async () => FakeRfb,
    );
    unmount();
    await Promise.resolve();
    await Promise.resolve();
    expect(FakeRfb.made).toEqual([]);
  });

  it("never disconnects noVNC again on unmount once the stream has closed", async () => {
    FakeRfb.made = [];
    const ch = createVncChannel(() => {});
    const unmount = mountVnc({} as HTMLElement, ch, async () => FakeRfb);
    await Promise.resolve();
    await Promise.resolve();
    const rfb = FakeRfb.made[0];
    ch.end(); // noVNC heard onclose: it is disconnected already
    unmount();
    expect(rfb.disconnected).toBe(false);
  });

  it("never mounts on a channel the relay already ended", async () => {
    FakeRfb.made = [];
    const ch = createVncChannel(() => {});
    ch.end();
    mountVnc({} as HTMLElement, ch, async () => FakeRfb);
    await Promise.resolve();
    await Promise.resolve();
    expect(FakeRfb.made).toEqual([]);
  });
});
