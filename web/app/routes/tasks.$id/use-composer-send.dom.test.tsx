/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// What the box's send does: an open question takes the text as its answer, a live task steers,
// a settled one continues the thread with an optimistic bubble (rolled back on failure), and
// each refusal lands on the right surface (strip, offer, or toast).
import { act, cleanup, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { toastManager } from "@prismoid/blocks/toast";
import type { Attachment } from "~/components/composer/capture";
import type { ComposerApi } from "~/components/composer/composer";
import type { QuestionDockHandle } from "~/components/tasks/question-dock";
import { ApiError, api } from "~/lib/api/client";
import { emptyView, type TaskView } from "~/lib/task-view";
import type { ScrollApi } from "./scroll-bridge";
import type { FollowUpDials } from "./use-follow-up-dials";
import { useComposerSend } from "./use-composer-send";
import type { Steer } from "./use-steer";

const ok = (data: unknown = { task_id: "t1" }) =>
  Promise.resolve({ data, error: undefined, response: new Response(null, { status: 200 }) });
const fail = (status: number, error: Record<string, unknown>) =>
  Promise.resolve({ data: undefined, error: { error }, response: new Response(null, { status }) });

let view: TaskView;
let post: ReturnType<typeof vi.spyOn>;
let toasts: ReturnType<typeof vi.spyOn>;
const steer = {
  stale: null as Steer["stale"],
  setStale: vi.fn(),
  sendSteer: vi.fn(),
  offerSteer: vi.fn(),
};
const dials = { wireFields: vi.fn(() => ({ size: "big" })), clearTurn: vi.fn() };
const answerCurrent = vi.fn();
const clear = vi.fn();
const toTurn = vi.fn();

const wrapper = ({ children }: { children: ReactNode }) => <MemoryRouter>{children}</MemoryRouter>;

function mount(over: { live?: boolean; questionOpen?: boolean } = {}) {
  return renderHook(
    () =>
      useComposerSend({
        task: { id: "t1", project_id: null },
        live: over.live ?? false,
        questionOpen: over.questionOpen ?? false,
        steerEpoch: 6,
        foldLocal: (fn) => {
          view = fn(view);
        },
        dials: dials as unknown as FollowUpDials,
        steer: steer as unknown as Steer,
        dockRef: { current: { answerCurrent } as unknown as QuestionDockHandle },
        scrollApi: { current: { toTurn } as unknown as ScrollApi },
        composerApi: { current: { clear } as unknown as ComposerApi },
      }),
    { wrapper },
  );
}

const body = () => post.mock.calls.at(-1)?.[1].body as Record<string, unknown>;
const titles = () => toasts.mock.calls.map((c: unknown[]) => (c[0] as { title: string }).title);
const send = (text: string, attachments: Attachment[] = []) => ({ text, attachments });

beforeEach(() => {
  view = emptyView();
  steer.stale = null;
  post = vi.spyOn(api, "POST").mockImplementation((() => ok()) as never);
  toasts = vi.spyOn(toastManager, "add").mockReturnValue("id");
  vi.stubGlobal("requestAnimationFrame", (cb: () => void) => (cb(), 1));
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  for (const m of [
    steer.setStale,
    steer.sendSteer,
    steer.offerSteer,
    dials.clearTurn,
    answerCurrent,
    clear,
    toTurn,
  ])
    m.mockReset();
});

describe("useComposerSend submit", () => {
  it("ignores empty text", async () => {
    const { result } = mount();
    await act(() => result.current.submit(send("   ")));
    expect(post).not.toHaveBeenCalled();
    expect(steer.sendSteer).not.toHaveBeenCalled();
  });

  it("an open question takes the trimmed text as its answer and sends nothing", async () => {
    const { result } = mount({ questionOpen: true });
    await act(() => result.current.submit(send("  2  ")));
    expect(answerCurrent).toHaveBeenCalledWith("2");
    expect(post).not.toHaveBeenCalled();
  });

  it("a live task steers instead of posting a follow-up", async () => {
    const { result } = mount({ live: true });
    await act(() => result.current.submit(send("hurry")));
    expect(steer.sendSteer).toHaveBeenCalledWith("hurry");
    expect(post).not.toHaveBeenCalled();
    expect(result.current.sending).toBe(false);
  });

  it("a settled task continues in place with the dials and ready attachments only", async () => {
    const files: Attachment[] = [
      {
        id: "a",
        filename: "a.txt",
        media_type: "text/plain",
        size: 1,
        content: "QQ",
        state: "ready",
      },
      {
        id: "b",
        filename: "b.txt",
        media_type: "text/plain",
        size: 1,
        content: "",
        state: "error",
      },
    ];
    const { result } = mount();
    const pending = act(() => result.current.submit(send("next", files)));
    expect(view.feed.at(-1)).toMatchObject({ kind: "user", text: "next", pending: true });
    await pending;
    expect(post.mock.calls[0][0]).toBe("/v1/tasks/{task_id}/follow_up");
    expect(body()).toMatchObject({
      goal: "next",
      clarify: "auto",
      size: "big",
      inputs: [{ filename: "a.txt", media_type: "text/plain", content: "QQ" }],
    });
    expect(toTurn).toHaveBeenCalled();
    expect(dials.clearTurn).toHaveBeenCalled();
  });

  it("a rejected follow-up rolls the bubble back, toasts and rethrows so the draft stays", async () => {
    post.mockImplementation((() => fail(500, { code: "x", message: "Broke." })) as never);
    const { result } = mount();
    await expect(act(() => result.current.submit(send("next")))).rejects.toBeInstanceOf(ApiError);
    expect(view.feed).toEqual([]);
    expect(titles()).toEqual(["Broke."]);
    expect(dials.clearTurn).not.toHaveBeenCalled();
  });

  it("a 422 on attachments reads as a sentence about the files", async () => {
    post.mockImplementation((() =>
      fail(422, {
        code: "validation_failed",
        message: "the request body is not valid",
        fields: { inputs: "List should have at most 10 items after validation, not 12" },
      })) as never);
    const { result } = mount();
    await expect(act(() => result.current.submit(send("next")))).rejects.toBeInstanceOf(ApiError);
    expect(titles()).toEqual(["A message takes up to 10 files. Remove some and send again."]);
  });

  it("a settled 409 offers the text as a steer", async () => {
    post.mockImplementation((() => fail(409, { code: "race", message: "Taken." })) as never);
    const { result } = mount();
    await expect(act(() => result.current.submit(send("mine")))).rejects.toBeDefined();
    expect(steer.offerSteer).toHaveBeenCalledWith("Taken.", "mine");
  });

  it("a stale steer on a live task draws the strip with what was missed", async () => {
    const missed = [{ kind: "turn", author: "Sam", epoch: 7 }];
    steer.sendSteer.mockRejectedValue(
      new ApiError(409, "stale_steer", "Stale", undefined, undefined, undefined, missed as never),
    );
    const { result } = mount({ live: true });
    await expect(act(() => result.current.submit(send("late")))).rejects.toBeDefined();
    expect(steer.setStale).toHaveBeenCalledWith({ text: "late", missed });
  });
});

describe("useComposerSend proposals and send anyway", () => {
  it("Start posts a follow-up carrying the deliverable kind", async () => {
    const { result } = mount();
    await act(() => result.current.startProposal(" Make a deck ", "deck"));
    expect(body()).toMatchObject({ goal: "Make a deck", deliverable: "deck" });
  });

  it("Start with an unknown deliverable sends none; while live it only says wait", async () => {
    const { result, rerender } = mount();
    await act(() => result.current.startProposal("Do it", "prismoid"));
    expect(body()).not.toHaveProperty("deliverable");
    post.mockClear();
    const live = mount({ live: true });
    await act(() => live.result.current.startProposal("Do it", null));
    expect(post).not.toHaveBeenCalled();
    expect(titles()).toEqual(["Wait for this turn to finish, then Start."]);
    rerender();
  });

  it("Start failure toasts", async () => {
    post.mockImplementation((() => fail(500, { code: "x", message: "Nope." })) as never);
    const { result } = mount();
    await act(() => result.current.startProposal("Do it", null));
    expect(titles()).toEqual(["Nope."]);
  });

  it("Send anyway re-sends the stale words at the current epoch and clears the box", async () => {
    steer.stale = { text: "words", missed: [] };
    steer.sendSteer.mockResolvedValue(undefined);
    const { result } = mount({ live: true });
    await act(() => result.current.sendAnyway());
    expect(steer.sendSteer).toHaveBeenCalledWith("words", { epoch: 6 });
    expect(clear).toHaveBeenCalledWith("words");
  });

  it("Send anyway refused again re-draws the strip; other errors toast; no strip does nothing", async () => {
    const { result } = mount({ live: true });
    await act(() => result.current.sendAnyway());
    expect(steer.sendSteer).not.toHaveBeenCalled();
    steer.stale = { text: "words", missed: [] };
    steer.sendSteer.mockRejectedValueOnce(new ApiError(409, "stale_steer", "Stale"));
    await act(() => result.current.sendAnyway());
    expect(steer.setStale).toHaveBeenCalledWith({ text: "words", missed: [] });
    steer.sendSteer.mockRejectedValueOnce(new ApiError(500, "x", "Server said no."));
    await act(() => result.current.sendAnyway());
    expect(titles()).toEqual(["Server said no."]);
    expect(clear).not.toHaveBeenCalled();
  });
});
