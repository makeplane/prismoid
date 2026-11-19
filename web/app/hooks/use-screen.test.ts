/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The screen socket's client: a fresh ticket per connect, the role's rate on
// open, paused while hidden, frames to bitmaps, and the close codes - final for 4410 and 4404,
// a backed-off reconnect for everything else. A fake WebSocket and a fake API stand in for the
// server; fake timers drive the backoff.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({ post: vi.fn() }));
vi.mock("~/lib/api/client", async (orig) => ({
  ...(await orig<typeof import("~/lib/api/client")>()),
  API_URL: "http://api.test",
  api: { POST: state.post },
  // The wire's failure is DATA here ({ err }) and unwrap raises it (the live-now.test idiom).
  unwrap: async (p: Promise<{ err?: Error }>) => {
    const v = await p;
    if (v?.err) throw v.err;
    return v;
  },
}));

import { ApiError } from "~/lib/api/client";
import { decode, encode, type Header } from "~/lib/screen-wire";
import {
  BACKOFF_MS,
  STOPPED_AFTER_MS,
  createScreenClient,
  currentLease,
  recheckScreens,
  type ScreenClient,
  type ScreenSnapshot,
} from "./use-screen";

class FakeSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;
  static all: FakeSocket[] = [];
  readyState = FakeSocket.CONNECTING;
  binaryType = "blob";
  sent: Header[] = [];
  closedWith: number | null = null;
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: unknown }) => void) | null = null;
  onclose: ((e: { code: number }) => void) | null = null;
  constructor(public url: string) {
    FakeSocket.all.push(this);
  }
  send(data: Uint8Array) {
    this.sent.push(
      decode(data.buffer.slice(data.byteOffset, data.byteOffset + data.byteLength) as ArrayBuffer)
        .header,
    );
  }
  close(code = 1000) {
    this.closedWith = code;
    this.readyState = FakeSocket.CLOSED;
  }
  // the server's side
  accept() {
    this.readyState = FakeSocket.OPEN;
    this.onopen?.();
  }
  deliver(header: Header, payload?: Uint8Array) {
    const b = encode(header, payload);
    this.onmessage?.({ data: b.buffer });
  }
  drop(code: number) {
    this.readyState = FakeSocket.CLOSED;
    this.onclose?.({ code });
  }
}

type FakeBitmap = { width: number; height: number; close: ReturnType<typeof vi.fn> };
const bitmaps: FakeBitmap[] = [];
const doc = {
  visibilityState: "visible" as "visible" | "hidden",
  listeners: new Set<() => void>(),
  addEventListener: (_: string, fn: () => void) => doc.listeners.add(fn),
  removeEventListener: (_: string, fn: () => void) => doc.listeners.delete(fn),
};

const flush = () => vi.advanceTimersByTimeAsync(0);
const last = () => FakeSocket.all[FakeSocket.all.length - 1];
let tickets = 0;

const made: ScreenClient[] = []; // every test's clients are stopped after it: no screen outlives its test

function client(pane = true) {
  const seen: ScreenSnapshot[] = [];
  const c = createScreenClient({
    taskId: "t1",
    conn: "tconn0001",
    pane,
    onState: (s) => seen.push(s),
  });
  made.push(c);
  const now = () => seen[seen.length - 1];
  return { c, seen, now };
}

beforeEach(() => {
  vi.useFakeTimers();
  FakeSocket.all = [];
  bitmaps.length = 0;
  doc.listeners.clear();
  doc.visibilityState = "visible";
  tickets = 0;
  state.post.mockReset();
  state.post.mockImplementation(async () => ({ ticket: `st_${++tickets}.sig`, expires_in: 30 }));
  vi.stubGlobal("WebSocket", FakeSocket);
  vi.stubGlobal("document", doc);
  vi.stubGlobal(
    "createImageBitmap",
    vi.fn(async () => {
      const b = { width: 1280, height: 900, close: vi.fn() };
      bitmaps.push(b);
      return b;
    }),
  );
});
afterEach(() => {
  for (const c of made.splice(0)) c.stop();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("connecting", () => {
  it("mints a ticket for its own conn and opens the socket with it, binary", async () => {
    const { c, now } = client();
    c.start();
    expect(now().status).toBe("connecting");
    await flush();
    expect(state.post).toHaveBeenCalledWith("/v1/tasks/{task_id}/screen/ticket", {
      params: { path: { task_id: "t1" } },
      body: { conn: "tconn0001" },
    });
    expect(last().url).toBe("ws://api.test/v1/tasks/t1/screen?ticket=st_1.sig&pane=1");
    expect(last().binaryType).toBe("arraybuffer");
  });

  it("says hello and asks for the role's most on open: the pane 30 fps, the thumbnail 1", async () => {
    const pane = client(true);
    pane.c.start();
    await flush();
    last().accept();
    expect(last().sent).toEqual([
      { t: "hello", v: 1, n: 0 },
      { t: "hint", fps: 30, paused: false, n: 0 },
    ]);
    const thumb = client(false);
    thumb.c.start();
    await flush();
    expect(last().url).toBe("ws://api.test/v1/tasks/t1/screen?ticket=st_2.sig");
    last().accept();
    expect(last().sent[1]).toMatchObject({ t: "hint", fps: 1 });
  });

  it("pauses while the tab is hidden, and resumes when it shows again", async () => {
    const { c } = client();
    c.start();
    await flush();
    last().accept();
    doc.visibilityState = "hidden";
    doc.listeners.forEach((fn) => fn());
    doc.visibilityState = "visible";
    doc.listeners.forEach((fn) => fn());
    expect(last().sent.slice(2)).toEqual([
      { t: "hint", paused: true, n: 0 },
      { t: "hint", paused: false, n: 0 },
    ]);
  });

  it("sends input only while the socket is open", async () => {
    const { c } = client();
    c.start();
    c.send({ t: "key", type: "down", key: "a" }); // no socket yet: dropped
    await flush();
    last().accept();
    c.send({ t: "text", text: "hunter2" });
    expect(last().sent.at(-1)).toEqual({ t: "text", text: "hunter2", n: 0 });
  });
});

describe("what the server says", () => {
  it("decodes a frame to a bitmap, closing the one it replaces", async () => {
    const { c, now } = client();
    c.start();
    await flush();
    last().accept();
    last().deliver(
      { t: "frame", seq: 1, ts: 0, w: 1280, h: 900, fmt: "jpeg" },
      new Uint8Array([1]),
    );
    await flush();
    expect(now()).toMatchObject({ status: "live", size: { w: 1280, h: 900 } });
    expect(now().frame).toBe(bitmaps[0]);
    last().deliver(
      { t: "frame", seq: 2, ts: 0, w: 1280, h: 900, fmt: "jpeg" },
      new Uint8Array([2]),
    );
    await flush();
    expect(now().frame).toBe(bitmaps[1]);
    expect(bitmaps[0].close).toHaveBeenCalled();
  });

  it("keeps the last frame under a mask, and the next frame lifts it", async () => {
    const { c, now } = client();
    c.start();
    await flush();
    last().accept();
    last().deliver({ t: "frame", w: 1280, h: 900 }, new Uint8Array([1]));
    await flush();
    last().deliver({ t: "masked", holder: "Bob" });
    expect(now()).toMatchObject({ masked: "Bob", frame: bitmaps[0] });
    last().deliver({ t: "masked", holder: { id: "b", name: "Bob" } });
    expect(now().masked).toBe("Bob");
    last().deliver({ t: "frame", w: 1280, h: 900 }, new Uint8Array([2]));
    await flush();
    expect(now().masked).toBeNull();
  });

  it("reads status: asleep drops the frame, starting, live, and busy", async () => {
    const { c, now } = client();
    c.start();
    await flush();
    last().accept();
    last().deliver({ t: "frame", w: 1280, h: 900 }, new Uint8Array([1]));
    await flush();
    last().deliver({ t: "status", upstream: "asleep" });
    expect(now()).toMatchObject({ status: "asleep", frame: null });
    expect(bitmaps[0].close).toHaveBeenCalled();
    last().deliver({ t: "status", chromium: "stopped", busy: false });
    expect(now().status).toBe("starting");
    last().deliver({ t: "status", chromium: "running", busy: true });
    expect(now()).toMatchObject({ status: "live", busy: true });
  });

  it("a Machine with no screen says so, with no picture, and never turns into stopped", async () => {
    // A Machine on a driver with no screen door: not "starting" - waiting changes nothing.
    const { c, now } = client();
    c.start();
    await flush();
    last().accept();
    last().deliver({ t: "frame", w: 1280, h: 900 }, new Uint8Array([1]));
    await flush();
    last().deliver({ t: "status", upstream: "no_screen" });
    expect(now()).toMatchObject({ status: "no_screen", frame: null, origin: null });
    await vi.advanceTimersByTimeAsync(STOPPED_AFTER_MS * 2);
    expect(now().status).toBe("no_screen");
  });

  it("keeps the page's origin as the server states it, and forgets it with the picture", async () => {
    const { c, now } = client();
    c.start();
    await flush();
    last().accept();
    expect(now().origin).toBeNull();
    last().deliver({
      t: "status",
      chromium: "running",
      busy: false,
      origin: "https://accounts.example.com",
    });
    expect(now().origin).toBe("https://accounts.example.com");
    last().deliver({ t: "status", busy: true }); // a status that names no origin leaves it be
    expect(now().origin).toBe("https://accounts.example.com");
    last().deliver({ t: "status", chromium: "running", busy: false, origin: "" });
    expect(now().origin).toBe("");
    last().deliver({
      t: "status",
      chromium: "running",
      busy: false,
      origin: "https://sso.example.org",
    });
    last().deliver({ t: "status", upstream: "asleep" });
    expect(now().origin).toBeNull();
  });

  it("says whether this connection drives as the server does, and forgets it while reconnecting", async () => {
    const { c, now } = client();
    c.start();
    await flush();
    last().accept();
    expect(now().driver).toBeNull();
    last().deliver({ t: "role", driver: true });
    expect(now().driver).toBe(true);
    last().deliver({ t: "role", driver: false });
    expect(now().driver).toBe(false);
    last().drop(1006);
    expect(now().driver).toBeNull();
  });

  it("a reset drops the old incarnation's picture: nothing of it is shown again", async () => {
    const { c, now } = client();
    c.start();
    await flush();
    last().accept();
    last().deliver({ t: "frame", seq: 1, w: 1280, h: 900, fmt: "jpeg" }, new Uint8Array([1]));
    await flush();
    expect(now().frame).not.toBeNull();
    last().deliver({ t: "reset" });
    expect(now().frame).toBeNull();
    expect(bitmaps[0].close).toHaveBeenCalled();
    last().deliver({ t: "frame", seq: 2, w: 1280, h: 900, fmt: "jpeg" }, new Uint8Array([2]));
    await flush();
    expect(now().frame).toBe(bitmaps[1]);
  });

  it("a frame still decoding when the picture went is never drawn, and never says live", async () => {
    let decoded: (b: FakeBitmap) => void = () => {};
    vi.stubGlobal(
      "createImageBitmap",
      vi.fn(() => new Promise<FakeBitmap>((resolve) => (decoded = resolve))),
    );
    const { c, now } = client();
    c.start();
    await flush();
    last().accept();
    last().deliver({ t: "frame", seq: 1, w: 1280, h: 900, fmt: "jpeg" }, new Uint8Array([1]));
    last().deliver({ t: "status", upstream: "asleep" });
    const late = { width: 1280, height: 900, close: vi.fn() };
    decoded(late);
    await flush();
    expect(now()).toMatchObject({ status: "asleep", frame: null });
    expect(late.close).toHaveBeenCalled();
  });

  it("says which origin the page could not reach, and forgets it once a page loads", async () => {
    const { c, now } = client();
    c.start();
    await flush();
    last().accept();
    expect(now().unreachable).toBeNull();
    last().deliver({
      t: "status",
      chromium: "running",
      busy: false,
      origin: "https://off.example",
      unreachable: "https://off.example",
    });
    expect(now().unreachable).toBe("https://off.example");
    last().deliver({ t: "status", busy: true }); // a status that names nothing leaves it be
    expect(now().unreachable).toBe("https://off.example");
    last().deliver({
      t: "status",
      chromium: "running",
      busy: false,
      origin: "https://ok.example",
      unreachable: "",
    });
    expect(now().unreachable).toBe("");
    last().deliver({
      t: "status",
      chromium: "running",
      busy: false,
      unreachable: "https://off.example",
    });
    last().deliver({ t: "status", upstream: "asleep" });
    expect(now().unreachable).toBeNull();
  });

  it("a start that never comes says the browser is stopped (UI check O7)", async () => {
    // An idle Machine whose browser was stopped: the bridge finds no browser until the agent's
    // next step, and the relay keeps saying "starting". Past STOPPED_AFTER_MS it is "stopped".
    const { c, now } = client();
    c.start();
    await flush();
    last().accept();
    last().deliver({ t: "status", upstream: "starting" });
    await vi.advanceTimersByTimeAsync(STOPPED_AFTER_MS - 1);
    expect(now().status).toBe("starting");
    await vi.advanceTimersByTimeAsync(1);
    expect(now().status).toBe("stopped");
    last().deliver({ t: "status", upstream: "live" });
    expect(now().status).toBe("live");
    // a start that lands in time never says stopped
    last().deliver({ t: "status", chromium: "stopped" });
    await vi.advanceTimersByTimeAsync(STOPPED_AFTER_MS / 2);
    last().deliver({ t: "frame", w: 1280, h: 900 }, new Uint8Array([1]));
    await flush();
    await vi.advanceTimersByTimeAsync(STOPPED_AFTER_MS);
    expect(now().status).toBe("live");
    // nor does one the socket's own close cuts short
    last().deliver({ t: "status", upstream: "starting" });
    last().drop(1006);
    await vi.advanceTimersByTimeAsync(STOPPED_AFTER_MS);
    expect(now().status).not.toBe("stopped");
  });

  it("drops a bitmap that finished decoding after a newer one", async () => {
    const pending: ((b: FakeBitmap) => void)[] = [];
    vi.stubGlobal("createImageBitmap", () => new Promise<FakeBitmap>((r) => pending.push(r)));
    const { c, now } = client();
    c.start();
    await flush();
    last().accept();
    last().deliver({ t: "frame", w: 1280, h: 900 }, new Uint8Array([1]));
    last().deliver({ t: "frame", w: 1280, h: 900 }, new Uint8Array([2]));
    const older = { width: 1, height: 1, close: vi.fn() };
    const newer = { width: 1, height: 1, close: vi.fn() };
    pending[1](newer);
    await flush();
    pending[0](older);
    await flush();
    expect(now().frame).toBe(newer);
    expect(older.close).toHaveBeenCalled();
  });

  it("never draws a frame whose decode began before a mask arrived, and closes it", async () => {
    const pending: ((b: FakeBitmap) => void)[] = [];
    vi.stubGlobal("createImageBitmap", () => new Promise<FakeBitmap>((r) => pending.push(r)));
    const { c, now } = client();
    c.start();
    await flush();
    last().accept();
    last().deliver({ t: "frame", w: 1280, h: 900 }, new Uint8Array([1]));
    last().deliver({ t: "masked", holder: { id: "b", name: "Bob" } });
    const late = { width: 1, height: 1, close: vi.fn() };
    pending[0](late);
    await flush();
    expect(late.close).toHaveBeenCalled();
    expect(now().frame).toBeNull();
    expect(now().masked).toBe("Bob");
  });

  it("an unnamed holder is still a mask, and a null holder lifts it", async () => {
    const { c, now } = client();
    c.start();
    await flush();
    last().accept();
    last().deliver({ t: "masked", holder: { id: "b", name: "" } });
    expect(now().masked).toBe("");
    last().deliver({ t: "masked", holder: null });
    expect(now().masked).toBeNull();
  });

  it("takes the bitmap's own size over a header that disagrees or asks for a huge canvas", async () => {
    const { c, now } = client();
    c.start();
    await flush();
    last().accept();
    last().deliver({ t: "frame", w: 999999, h: 900 }, new Uint8Array([1]));
    await flush();
    expect(now().size).toEqual({ w: 1280, h: 900 });
  });

  it("skips a malformed message", async () => {
    const { c, seen } = client();
    c.start();
    await flush();
    last().accept();
    const before = seen.length;
    last().onmessage?.({ data: new Uint8Array([0, 0, 0, 9, 1]).buffer });
    last().onmessage?.({ data: "text is not the screen's" });
    expect(seen.length).toBe(before);
  });
});

describe("closing and reconnecting", () => {
  it("4410 is off and 4404 is closed, and neither reconnects", async () => {
    const off = client();
    off.c.start();
    await flush();
    last().drop(4410);
    expect(off.now().status).toBe("off");
    const gone = client();
    gone.c.start();
    await flush();
    last().drop(4404);
    expect(gone.now().status).toBe("closed");
    await vi.advanceTimersByTimeAsync(60_000);
    expect(state.post).toHaveBeenCalledTimes(2);
  });

  it("off and closed drop the last frame and close its bitmap (UI check D4)", async () => {
    for (const code of [4410, 4404]) {
      const { c, now } = client();
      c.start();
      await flush();
      last().accept();
      last().deliver({ t: "frame", w: 1280, h: 900 }, new Uint8Array([1]));
      await flush();
      const shown = bitmaps[bitmaps.length - 1];
      expect(now().frame).toBe(shown);
      last().drop(code);
      expect(now()).toMatchObject({ frame: null, size: null });
      expect(shown.close).toHaveBeenCalled();
    }
  });

  it("anything else reconnects with a NEW ticket, backing off 0.5 s doubling to 10 s", async () => {
    const { c, now } = client();
    c.start();
    await flush();
    const waits = [500, 1000, 2000, 4000, 8000, 10_000, 10_000];
    for (const [i, wait] of waits.entries()) {
      last().drop(1006);
      expect(now().status).toBe("connecting");
      await vi.advanceTimersByTimeAsync(wait - 1);
      expect(state.post).toHaveBeenCalledTimes(i + 1);
      await vi.advanceTimersByTimeAsync(1);
      expect(state.post).toHaveBeenCalledTimes(i + 2);
      expect(last().url).toContain(`ticket=st_${i + 2}.sig`);
    }
    expect(BACKOFF_MS.max).toBe(10_000);
  });

  it("a message from the server starts the backoff over", async () => {
    const { c } = client();
    c.start();
    await flush();
    last().drop(4401);
    await vi.advanceTimersByTimeAsync(500);
    last().drop(4408);
    await vi.advanceTimersByTimeAsync(1000);
    last().accept();
    last().deliver({ t: "status", chromium: "running", busy: false });
    last().drop(1006);
    await vi.advanceTimersByTimeAsync(500);
    expect(state.post).toHaveBeenCalledTimes(4);
  });

  it("a refusal's hello and bye never start the backoff over: a server that keeps refusing is backed off", async () => {
    const { c } = client();
    c.start();
    await flush();
    for (const wait of [500, 1000, 2000, 4000]) {
      last().accept();
      last().deliver({ t: "hello", v: 1 });
      last().deliver({ t: "bye", reason: "unauthorized" });
      last().drop(4401);
      const before = state.post.mock.calls.length;
      await vi.advanceTimersByTimeAsync(wait - 1);
      expect(state.post).toHaveBeenCalledTimes(before);
      await vi.advanceTimersByTimeAsync(1);
      expect(state.post).toHaveBeenCalledTimes(before + 1);
    }
  });

  it("a ticket refused with 409 is off, with 404 is closed; any other failure retries", async () => {
    state.post.mockImplementationOnce(async () => ({
      err: new ApiError(409, "conflict", "Computer use is off."),
    }));
    const off = client();
    off.c.start();
    await flush();
    expect(off.now().status).toBe("off");
    state.post.mockImplementationOnce(async () => ({
      err: new ApiError(404, "not_found", "no such task"),
    }));
    const gone = client();
    gone.c.start();
    await flush();
    expect(gone.now().status).toBe("closed");
    state.post.mockImplementationOnce(async () => ({ err: new Error("network") }));
    const flaky = client();
    flaky.c.start();
    await flush();
    expect(flaky.now().status).toBe("connecting");
    await vi.advanceTimersByTimeAsync(500);
    expect(last().url).toContain("ticket=st_");
  });

  it("off asks again when the page's stream comes back: only a restart moves the switch", async () => {
    state.post.mockImplementationOnce(async () => ({
      err: new ApiError(409, "conflict", "Computer use is off."),
    }));
    const { c, now } = client();
    c.start();
    await flush();
    expect(now().status).toBe("off");
    await vi.advanceTimersByTimeAsync(60_000);
    expect(state.post).toHaveBeenCalledTimes(1); // final: nothing asks again on its own
    recheckScreens();
    await flush();
    expect(state.post).toHaveBeenCalledTimes(2);
    last().accept();
    last().deliver({ t: "status", upstream: "live" });
    expect(now().status).toBe("live");
    c.stop();
  });

  it("an off socket close (4410) is rechecked too, and a screen that is not off ignores it", async () => {
    const off = client();
    off.c.start();
    await flush();
    last().accept();
    last().drop(4410);
    expect(off.now().status).toBe("off");
    const live = client();
    live.c.start();
    await flush();
    last().accept();
    last().deliver({ t: "status", upstream: "live" });
    const before = state.post.mock.calls.length;
    recheckScreens();
    await flush();
    expect(state.post).toHaveBeenCalledTimes(before + 1); // the off one only
    off.c.stop();
    live.c.stop();
  });

  it("a stopped screen is never rechecked", async () => {
    state.post.mockImplementationOnce(async () => ({
      err: new ApiError(409, "conflict", "Computer use is off."),
    }));
    const { c } = client();
    c.start();
    await flush();
    c.stop();
    recheckScreens();
    await flush();
    expect(state.post).toHaveBeenCalledTimes(1);
  });

  it("stop closes the socket, lets go of the tab, closes the bitmap and never reconnects", async () => {
    const { c } = client();
    c.start();
    await flush();
    const sock = last();
    sock.accept();
    sock.deliver({ t: "frame", w: 1280, h: 900 }, new Uint8Array([1]));
    await flush();
    c.stop();
    expect(sock.closedWith).toBe(1000);
    expect(doc.listeners.size).toBe(0);
    expect(bitmaps[0].close).toHaveBeenCalled();
    sock.drop(1006);
    await vi.advanceTimersByTimeAsync(30_000);
    expect(state.post).toHaveBeenCalledTimes(1);
  });
});

describe("currentLease", () => {
  const T = Date.parse("2026-10-02T12:00:00Z");
  const ada = { id: "ada", name: "Ada" };
  const until = (s: number) => new Date(T + s * 1000).toISOString();

  it("nobody holds it: null stays null", () => {
    expect(currentLease(null, T)).toBeNull();
  });

  it("never expires a lease on its own clock: until in the past is still the holder", () => {
    expect(currentLease({ holder: ada, private: true, until: until(-60) }, T)?.holder.name).toBe(
      "Ada",
    );
    expect(currentLease({ holder: ada, private: false, until: null }, T)?.holder.name).toBe("Ada");
  });
});

describe("a control-stream driver: vnc and the cursor", () => {
  async function started(o: { pane: boolean }) {
    const { c, seen: snaps } = client(o.pane);
    c.start();
    await flush();
    const sock = last();
    sock.accept();
    return { c, snaps, sock };
  }

  it("a vnc stream opens a channel, its bytes reach it, and its end closes it", async () => {
    const { c, snaps, sock } = await started({ pane: true });
    sock.deliver({ t: "vnc", state: "open" });
    const ch = snaps.at(-1)!.vnc!;
    expect(ch.readyState).toBe("open");
    const got: string[] = [];
    ch.onmessage = (e) => got.push(new TextDecoder().decode(e.data));
    sock.deliver({ t: "vnc" }, new TextEncoder().encode("RFB 003.008\n"));
    await Promise.resolve();
    expect(got).toEqual(["RFB 003.008\n"]);
    sock.deliver({ t: "vnc", state: "closed" });
    expect(ch.readyState).toBe("closed");
    expect(snaps.at(-1)!.vnc).toBeNull();
    c.stop();
  });

  it("the channel's sends go up as vnc messages with their payload", async () => {
    const { c, snaps, sock } = await started({ pane: true });
    sock.deliver({ t: "vnc", state: "open" });
    snaps.at(-1)!.vnc!.send(new Uint8Array([5, 0, 0, 1, 0, 1]));
    expect(sock.sent.at(-1)).toEqual({ t: "vnc", n: 6 });
    c.stop();
  });

  it("a dropped socket ends the channel", async () => {
    const { snaps, sock } = await started({ pane: true });
    sock.deliver({ t: "vnc", state: "open" });
    const ch = snaps.at(-1)!.vnc!;
    sock.onclose?.({ code: 1006 });
    expect(ch.readyState).toBe("closed");
    expect(snaps.at(-1)!.vnc).toBeNull();
  });

  it("the cursor's shape and place, and the mask taking them away", async () => {
    // property spies on the real URL, restored in afterEach: no later test runs on a fake
    for (const k of ["createObjectURL", "revokeObjectURL"] as const) {
      if (!(k in URL))
        Object.defineProperty(URL, k, { value: () => "", configurable: true, writable: true });
    }
    vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:x");
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
    const { c, snaps, sock } = await started({ pane: true });
    sock.deliver(
      { t: "cursor", serial: 1, w: 24, h: 24, hot_x: 1, hot_y: 2 },
      new Uint8Array([0x89, 0x50]),
    );
    sock.deliver({ t: "cursor", serial: 1, x: 100, y: 50 });
    const cur = snaps.at(-1)!.cursor!;
    expect(cur.x).toBe(100);
    expect(cur.shape).toMatchObject({ w: 24, h: 24, hotX: 1, hotY: 2 });
    sock.deliver({ t: "masked", holder: { id: "b", name: "Bob" } });
    expect(snaps.at(-1)!.cursor).toBeNull();
    c.stop();
  });
});
