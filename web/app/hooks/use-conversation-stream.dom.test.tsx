/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// The conversation socket: it opens with a single-use ticket (never the session token) and the
// cursor, folds durable message rows and the live reply deltas, ignores malformed frames, reports
// connected state, polls the record while down, reopens a dropped socket with a fresh ticket, and
// closes on unmount.
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "~/lib/api/client";
import type { MessageOut } from "~/lib/chat-stream";
import { STORAGE } from "~/lib/storage";
import { useConversationStream } from "./use-conversation-stream";

class FakeSource {
  static CLOSED = 2;
  static instances: FakeSource[] = [];
  readyState = 1;
  closed = false;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  listeners = new Map<string, (e: MessageEvent) => void>();
  constructor(public url: string) {
    FakeSource.instances.push(this);
  }
  addEventListener(type: string, fn: (e: MessageEvent) => void) {
    this.listeners.set(type, fn);
  }
  close() {
    this.closed = true;
    this.readyState = FakeSource.CLOSED;
  }
  emit(type: string, data: unknown) {
    const raw = typeof data === "string" ? data : JSON.stringify(data);
    this.listeners.get(type)?.({ data: raw } as MessageEvent);
  }
}

const row = (seq: number, over: Partial<MessageOut> = {}): MessageOut =>
  ({ seq, role: "user", kind: "text", text: `line ${seq}`, task_id: null, ...over }) as MessageOut;

const last = () => FakeSource.instances.at(-1) as FakeSource;
let get: ReturnType<typeof vi.spyOn>;
let post: ReturnType<typeof vi.spyOn>;
let minted = 0;

/** Let the ticket mint answer and the source open: a few microtasks, no timers. */
async function opened(): Promise<FakeSource> {
  for (let i = 0; i < 20; i++) {
    await act(async () => {
      await Promise.resolve();
    });
  }
  return last();
}

async function mount(id = "c1") {
  const hook = renderHook(() => useConversationStream(id));
  await opened();
  return hook;
}

beforeEach(() => {
  FakeSource.instances = [];
  vi.stubGlobal("EventSource", FakeSource);
  vi.stubGlobal("requestAnimationFrame", (cb: () => void) => (cb(), 1));
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "Date"] });
  minted = 0;
  post = vi.spyOn(api, "POST").mockImplementation((() =>
    Promise.resolve({
      data: { ticket: `et_ticket_${++minted}`, expires_in: 30 },
      error: undefined,
      response: new Response(null, { status: 200 }),
    })) as never);
  get = vi.spyOn(api, "GET").mockImplementation((() =>
    Promise.resolve({
      data: [row(1)],
      error: undefined,
      response: new Response(null, { status: 200 }),
    })) as never);
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("useConversationStream", () => {
  it("opens the events URL for the conversation from cursor 0 with a ticket, never the token", async () => {
    localStorage.setItem(STORAGE.token, "cs_secret");
    await mount();
    expect(post).toHaveBeenCalledWith("/v1/conversations/{conversation_id}/events/ticket", {
      params: { path: { conversation_id: "c1" } },
    });
    expect(last().url).toContain("/v1/conversations/c1/events?since=0&ticket=et_ticket_1");
    expect(last().url).not.toContain("token=");
    expect(last().url).not.toContain("cs_secret");
  });

  it("becomes connected and ready when the socket opens", async () => {
    const { result } = await mount();
    expect(result.current.connected).toBe(false);
    act(() => last().onopen?.());
    expect(result.current.connected).toBe(true);
    expect(result.current.ready).toBe(true);
  });

  it("folds message rows in seq order, and an edit of a seq replaces the row", async () => {
    const { result } = await mount();
    act(() => {
      last().emit("message", row(2));
      last().emit("message", row(1));
    });
    expect(result.current.state.rows.map((r) => r.seq)).toEqual([1, 2]);
    act(() => last().emit("message", row(2, { text: "edited" })));
    expect(result.current.state.rows[1].text).toBe("edited");
    expect(result.current.state.lastSeq).toBe(2);
  });

  it("streams the live reply, and the bot's durable line retires it", async () => {
    const { result } = await mount();
    act(() => {
      last().emit("chat.start", { type: "chat.start", bot: "Prismoid", after_seq: 0 });
      last().emit("chat.delta", { type: "chat.delta", text: "Hel", after_seq: 0 });
      last().emit("chat.delta", { type: "chat.delta", text: "lo", after_seq: 0 });
    });
    expect(result.current.state.live?.text).toBe("Hello");
    act(() => last().emit("message", row(1, { role: "bot", text: "Hello" })));
    expect(result.current.state.live).toBeNull();
  });

  it("drops a malformed frame without tearing the stream down", async () => {
    const { result } = await mount();
    act(() => last().emit("message", "{not json"));
    act(() => last().emit("message", row(1)));
    expect(result.current.state.rows).toHaveLength(1);
  });

  it("an error closes the socket (its ticket is spent) and marks it disconnected", async () => {
    const { result } = await mount();
    act(() => last().onopen?.());
    act(() => last().onerror?.());
    expect(result.current.connected).toBe(false);
    expect(last().closed).toBe(true);
  });

  it("while down it polls the record past the cursor and folds the answer", async () => {
    const { result } = await mount();
    await act(() => vi.advanceTimersByTimeAsync(3000));
    expect(get.mock.calls[0][1].params.query).toEqual({ since: 0 });
    expect(result.current.state.rows.map((r) => r.seq)).toEqual([1]);
    expect(result.current.ready).toBe(true);
  });

  it("does not poll while the socket is open", async () => {
    await mount();
    act(() => last().onopen?.());
    await act(() => vi.advanceTimersByTimeAsync(9000));
    expect(get).not.toHaveBeenCalled();
  });

  it("reopens a dropped socket after the back-off with a fresh ticket, asking from the newest seq", async () => {
    await mount();
    act(() => last().emit("message", row(4)));
    const first = last();
    act(() => first.onerror?.());
    expect(first.closed).toBe(true);
    await act(() => vi.advanceTimersByTimeAsync(6000));
    await opened();
    expect(FakeSource.instances).toHaveLength(2);
    expect(last().url).toContain("since=4");
    expect(last().url).toContain("ticket=et_ticket_2");
  });

  it("refresh reads on demand, and foldRows edits the held rows", async () => {
    const { result } = await mount();
    await act(() => result.current.refresh());
    expect(result.current.state.rows).toHaveLength(1);
    act(() => result.current.foldRows((rows) => rows.map((r) => ({ ...r, status: "done" }))));
    expect(result.current.state.rows[0].status).toBe("done");
  });

  it("closes the socket on unmount and starts clean for another conversation", async () => {
    const { result, rerender, unmount } = renderHook(({ id }) => useConversationStream(id), {
      initialProps: { id: "c1" },
    });
    await opened();
    act(() => last().emit("message", row(1)));
    const first = last();
    rerender({ id: "c2" });
    await opened();
    expect(first.closed).toBe(true);
    expect(last().url).toContain("/v1/conversations/c2/events");
    expect(result.current.state.rows).toEqual([]);
    unmount();
    expect(last().closed).toBe(true);
  });
});
