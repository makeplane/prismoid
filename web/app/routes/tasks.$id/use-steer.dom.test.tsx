/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// The steer hook: a send posts the draft with its command id and epoch, folds the new epoch
// into the view, clears the stale strip, and a refused "Send as steer" lands on the strip or a
// toast depending on whether the box still holds those words.
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { toastManager } from "@prismoid/blocks/toast";
import type { ComposerApi } from "~/components/composer/composer";
import { ApiError, api } from "~/lib/api/client";
import { emptyView, type TaskView } from "~/lib/task-view";
import type { ScrollApi } from "./scroll-bridge";
import { useSteer } from "./use-steer";

const ok = (data: unknown) =>
  Promise.resolve({ data, error: undefined, response: new Response(null, { status: 200 }) });
const fail = (status: number, error: Record<string, unknown>) =>
  Promise.resolve({ data: undefined, error: { error }, response: new Response(null, { status }) });

let view: TaskView;
let post: ReturnType<typeof vi.spyOn>;
let toasts: ReturnType<typeof vi.spyOn>;
const clear = vi.fn();
const toEnd = vi.fn();

function mount(over: { held?: boolean; epoch?: number } = {}) {
  return renderHook(() =>
    useSteer({
      taskId: "t1",
      steerEpoch: over.epoch ?? 4,
      held: over.held ?? false,
      foldLocal: (fn) => {
        view = fn(view);
      },
      onText: vi.fn(),
      scrollApi: { current: { toEnd } as unknown as ScrollApi },
      composerApi: { current: { clear } as unknown as ComposerApi },
    }),
  );
}

const lastBody = () => post.mock.calls.at(-1)?.[1].body as Record<string, unknown>;
const titles = () => toasts.mock.calls.map((c: unknown[]) => (c[0] as { title: string }).title);

beforeEach(() => {
  view = { ...emptyView(), steerEpoch: 4 };
  post = vi.spyOn(api, "POST").mockImplementation((() => ok({ epoch: 5 })) as never);
  toasts = vi.spyOn(toastManager, "add").mockReturnValue("id");
  vi.stubGlobal("requestAnimationFrame", (cb: () => void) => (cb(), 1));
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  clear.mockReset();
  toEnd.mockReset();
});

describe("useSteer", () => {
  it("posts the directive stamped with the typing epoch and folds the new epoch in", async () => {
    const { result } = mount();
    act(() => result.current.onDraft("go faster"));
    await act(() => result.current.sendSteer("go faster"));
    expect(post.mock.calls[0][0]).toBe("/v1/tasks/{task_id}/steer");
    expect(lastBody()).toMatchObject({ directive: "go faster", issued_epoch: 4 });
    expect(typeof lastBody().command_id).toBe("string");
    expect(view.steerEpoch).toBe(5);
    expect(toEnd).toHaveBeenCalled();
    expect(titles()).toEqual([]);
  });

  it("resending the same draft keeps its command id; edited words mint another", async () => {
    post.mockImplementation((() => Promise.reject(new Error("lost"))) as never);
    const { result } = mount();
    act(() => result.current.onDraft("one"));
    await act(() => result.current.sendSteer("one").catch(() => undefined));
    const first = lastBody().command_id;
    await act(() => result.current.sendSteer("one").catch(() => undefined));
    expect(lastBody().command_id).toBe(first);
    act(() => result.current.onDraft("one two"));
    await act(() => result.current.sendSteer("one two").catch(() => undefined));
    expect(lastBody().command_id).not.toBe(first);
  });

  it("an explicit epoch overrides the typing stamp, and a held fork says it launched", async () => {
    const { result } = mount({ held: true });
    act(() => result.current.onDraft("start"));
    await act(() => result.current.sendSteer("start", { epoch: 9 }));
    expect(lastBody().issued_epoch).toBe(9);
    expect(titles()).toEqual(["Branch launched"]);
  });

  it("does not lower the epoch when the answer is older than the view", async () => {
    view = { ...emptyView(), steerEpoch: 12 };
    const { result } = mount();
    await act(() => result.current.sendSteer("late"));
    expect(view.steerEpoch).toBe(12);
  });

  it("a failed send throws to its caller and leaves the strip alone", async () => {
    post.mockImplementation((() => fail(500, { code: "boom", message: "No." })) as never);
    const { result } = mount();
    await expect(act(() => result.current.sendSteer("x"))).rejects.toBeInstanceOf(ApiError);
    expect(result.current.stale).toBeNull();
  });

  it("the strip closes when the box stops holding the refused words, and discard clears the box", () => {
    const { result } = mount();
    act(() => result.current.setStale({ text: "old words", missed: [] }));
    act(() => result.current.onDraft("old words"));
    expect(result.current.stale).not.toBeNull();
    act(() => result.current.onDraft("new words"));
    expect(result.current.stale).toBeNull();
    act(() => result.current.setStale({ text: "again", missed: [] }));
    act(() => result.current.discardStale());
    expect(clear).toHaveBeenCalledWith("again");
    expect(result.current.stale).toBeNull();
  });

  describe("offerSteer", () => {
    const action = () => {
      const opts = toasts.mock.calls[0][0] as {
        data: { primaryAction: { label: string; onClick: () => void } };
      };
      return opts.data.primaryAction;
    };

    it("offers Send as steer, which sends against the current epoch and clears the box", async () => {
      const { result } = mount({ epoch: 7 });
      act(() => result.current.offerSteer("Someone else won.", "my words"));
      expect(titles()).toEqual(["Someone else won."]);
      expect(action().label).toBe("Send as steer");
      await act(async () => action().onClick());
      expect(lastBody()).toMatchObject({ directive: "my words", issued_epoch: 7 });
      expect(clear).toHaveBeenCalledWith("my words");
    });

    it("a stale refusal while the box holds the words draws the strip", async () => {
      post.mockImplementation((() =>
        fail(409, { code: "stale_steer", message: "Stale", missed: [] })) as never);
      const { result } = mount();
      act(() => result.current.onDraft("my words"));
      act(() => result.current.offerSteer("Lost.", "my words"));
      await act(async () => action().onClick());
      expect(result.current.stale?.text).toBe("my words");
      expect(clear).not.toHaveBeenCalled();
    });

    it("a refusal after the person typed on is a toast, never nothing", async () => {
      post.mockImplementation((() =>
        fail(409, { code: "stale_steer", message: "Stale again" })) as never);
      const { result } = mount();
      act(() => result.current.onDraft("something else"));
      act(() => result.current.offerSteer("Lost.", "my words"));
      await act(async () => action().onClick());
      expect(result.current.stale).toBeNull();
      expect(titles()).toContain("Stale again");
    });
  });
});
