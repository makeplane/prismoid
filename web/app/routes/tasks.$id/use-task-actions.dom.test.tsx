/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// The task's one-shot verbs: each posts to its own path, fork navigates to the child, the
// thumbs paint at once and revert on failure, and a failed verb says so in a toast.
import { act, cleanup, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { toastManager } from "@prismoid/blocks/toast";
import { api } from "~/lib/api/client";
import { emptyView, type TaskView } from "~/lib/task-view";
import { useTaskActions } from "./use-task-actions";

const ok = (data: unknown = {}) =>
  Promise.resolve({ data, error: undefined, response: new Response(null, { status: 200 }) });
const fail = (message: string) =>
  Promise.resolve({
    data: undefined,
    error: { error: { code: "nope", message } },
    response: new Response(null, { status: 500 }),
  });

let view: TaskView;
let post: ReturnType<typeof vi.spyOn>;
let toasts: ReturnType<typeof vi.spyOn>;
const revalidate = vi.fn();

function Where() {
  return <p data-testid="where">{useLocation().pathname}</p>;
}
const wrapper = ({ children }: { children: ReactNode }) => (
  <MemoryRouter initialEntries={["/start"]}>
    <Routes>
      <Route path="*" element={children} />
    </Routes>
    <Where />
  </MemoryRouter>
);

function mount(feedback: TaskView["feedback"] = null) {
  return renderHook(
    () =>
      useTaskActions({
        task: { id: "t1", project_id: null },
        feedback,
        foldLocal: (fn) => {
          view = fn(view);
        },
        revalidate,
      }),
    { wrapper },
  );
}

const paths = () => post.mock.calls.map((c: unknown[]) => c[0]);
const titles = () => toasts.mock.calls.map((c: unknown[]) => (c[0] as { title: string }).title);

beforeEach(() => {
  view = emptyView();
  post = vi.spyOn(api, "POST").mockImplementation((() => ok()) as never);
  toasts = vi.spyOn(toastManager, "add").mockReturnValue("id");
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  revalidate.mockReset();
});

describe("useTaskActions", () => {
  it("pause, resume and cancel each hit their own path", async () => {
    const { result } = mount();
    await act(() => result.current.control("pause"));
    await act(() => result.current.control("resume"));
    await act(() => result.current.control("cancel"));
    expect(paths()).toEqual([
      "/v1/tasks/{task_id}/pause",
      "/v1/tasks/{task_id}/resume",
      "/v1/tasks/{task_id}/cancel",
    ]);
    expect(post.mock.calls[0][1].params.path).toEqual({ task_id: "t1" });
  });

  it("a failed control says why in a toast", async () => {
    post.mockImplementation((() => fail("Cannot pause now.")) as never);
    const { result } = mount();
    await act(() => result.current.control("pause"));
    expect(titles()).toEqual(["Cannot pause now."]);
  });

  it("sleep and reset the Machine re-read the task; a failure uses its own words", async () => {
    const { result } = mount();
    await act(() => result.current.machineAction("sleep"));
    await act(() => result.current.machineAction("reset"));
    expect(paths()).toEqual([
      "/v1/tasks/{task_id}/machine/sleep",
      "/v1/tasks/{task_id}/machine/reset",
    ]);
    expect(revalidate).toHaveBeenCalledTimes(2);
    post.mockImplementation((() => Promise.reject(new Error("x"))) as never);
    await act(() => result.current.machineAction("sleep"));
    await act(() => result.current.machineAction("reset"));
    expect(titles()).toEqual([
      "Could not put the Machine to sleep.",
      "Could not reset the Machine.",
    ]);
    expect(revalidate).toHaveBeenCalledTimes(2);
  });

  it("the thumbs paint now and record the rating", async () => {
    const { result } = mount();
    await act(() => result.current.sendFeedback("up"));
    expect(view.feedback).toBe("up");
    expect(post.mock.calls[0][1].body).toEqual({ rating: "up" });
  });

  it("the thumbs revert to the previous rating when the record fails", async () => {
    post.mockImplementation((() => fail("Could not store.")) as never);
    const { result } = mount("down");
    await act(() => result.current.sendFeedback("up"));
    expect(view.feedback).toBe("down");
    expect(titles()).toEqual(["Could not store."]);
  });

  it("retry posts a client token and releases its latch", async () => {
    const { result } = mount();
    await act(() => result.current.retry());
    expect(post.mock.calls[0][0]).toBe("/v1/tasks/{task_id}/retry");
    expect(typeof post.mock.calls[0][1].body.client_token).toBe("string");
    expect(result.current.retrying).toBe(false);
    post.mockImplementation((() => fail("Busy.")) as never);
    await act(() => result.current.retry());
    expect(titles()).toEqual(["Busy."]);
  });

  it("fork posts once and navigates to the child task", async () => {
    post.mockImplementation((() => ok({ task_id: "child9" })) as never);
    const { result } = mount();
    await act(() => result.current.fork());
    expect(post.mock.calls[0][0]).toBe("/v1/tasks/{task_id}/fork");
    expect(document.querySelector('[data-testid="where"]')?.textContent).toContain("child9");
  });

  it("a failed fork stays put and toasts", async () => {
    post.mockImplementation((() => fail("No fork.")) as never);
    const { result } = mount();
    await act(() => result.current.fork());
    expect(document.querySelector('[data-testid="where"]')?.textContent).toBe("/start");
    expect(titles()).toEqual(["No fork."]);
  });

  it("retract names the steer's command id; a lost race shows the server's words", async () => {
    const { result } = mount();
    await act(() => result.current.retractSteer("cmd1"));
    expect(post.mock.calls[0][1].params.path).toEqual({ task_id: "t1", command_id: "cmd1" });
    post.mockImplementation((() => fail("Already applied.")) as never);
    await act(() => result.current.retractSteer("cmd1"));
    expect(titles()).toEqual(["Already applied."]);
  });

  it("declining a proposal closes that row locally, with no request", () => {
    view = {
      ...emptyView(),
      feed: [{ kind: "proposal", id: "p1", open: true } as unknown as TaskView["feed"][number]],
    };
    const { result } = mount();
    act(() => result.current.declineProposalRow("p1"));
    expect((view.feed[0] as unknown as { open: boolean }).open).toBe(false);
    expect(post).not.toHaveBeenCalled();
  });
});
