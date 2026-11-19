/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// The question dock, driven: clicking an option answers and advances, the last answer submits
// the whole batch to the gate, Skip declines, required questions refuse silence, a reply typed
// in the composer lands through the handle, and the dock folds away and back.
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createRef } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "@prismoid/blocks/toast";
import { api, ApiError } from "~/lib/api/client";
import type { Question } from "~/lib/api/events";
import type { GateView } from "~/lib/task-view";
import { QuestionDock, type QuestionDockHandle } from "./question-dock";

const runtime: Question = {
  id: "runtime",
  prompt: "Where will this run?",
  type: "single_select",
  required: true,
  allow_other: false,
  options: [
    { value: "kvm", label: "Bare-metal Linux", description: null },
    { value: "cloud", label: "Cloud VM", description: null },
  ],
};
const name: Question = {
  id: "name",
  prompt: "What should it be called?",
  type: "text",
  required: false,
  allow_other: false,
};
const tools: Question = {
  id: "tools",
  prompt: "Which tools?",
  type: "multi_select",
  required: false,
  allow_other: false,
  options: [
    { value: "git", label: "Git", description: null },
    { value: "make", label: "Make", description: null },
  ],
};

const gate = (...questions: Question[]) => ({ gate_id: "g1", questions }) as unknown as GateView;
const resolved = { resolved_by: "Ada", resolved_by_id: "u1", action: "accept" };
const reply = (data: unknown) =>
  Promise.resolve({ data, error: undefined, response: new Response(null, { status: 200 }) });

let post: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  post = vi.spyOn(api, "POST").mockImplementation((() => reply(resolved)) as never);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const bodyOf = () => (post.mock.calls[0][1] as { body: unknown }).body;

describe("QuestionDock", () => {
  it("renders nothing for a gate with no questions", () => {
    const { container } = render(<QuestionDock gate={gate()} />);
    expect(container.innerHTML).toBe("");
  });

  it("a click answers and advances, and the last answer submits the batch", async () => {
    const user = userEvent.setup();
    const onResolved = vi.fn();
    render(<QuestionDock gate={gate(runtime, name)} onResolved={onResolved} />);
    expect(screen.getByText("1 of 2")).toBeDefined();
    await user.click(screen.getByRole("button", { name: /Cloud VM/ }));
    expect(screen.getByText("What should it be called?")).toBeDefined();
    expect(post).not.toHaveBeenCalled();
    await user.type(screen.getByPlaceholderText("Type your answer…"), "Atlas{Enter}");
    await waitFor(() => expect(onResolved).toHaveBeenCalledWith("Ada", "u1", "accept"));
    expect(post.mock.calls[0][1]).toMatchObject({
      params: { path: { gate_id: "g1" } },
      body: { action: "accept", content: { runtime: "cloud", name: "Atlas" } },
    });
  });

  // Skip used to decline the whole set and drop every typed answer: it now skips one
  // question, and "Use defaults" is the decline - with what was answered riding along.
  it("Use defaults declines the rest and keeps what was answered", async () => {
    const user = userEvent.setup();
    render(<QuestionDock gate={gate(runtime, name)} />);
    await user.click(screen.getByRole("button", { name: /Cloud VM/ }));
    await user.click(screen.getByRole("button", { name: "Use defaults" }));
    await waitFor(() => expect(post).toHaveBeenCalled());
    expect(bodyOf()).toEqual({ action: "decline", content: { runtime: "cloud" } });
  });

  it("Use defaults with nothing answered sends a bare decline", async () => {
    const user = userEvent.setup();
    render(<QuestionDock gate={gate(runtime)} />);
    await user.click(screen.getByRole("button", { name: "Use defaults" }));
    await waitFor(() => expect(post).toHaveBeenCalled());
    expect(bodyOf()).toEqual({ action: "decline" });
  });

  it("the stepper revisits an earlier question, and an unsent typed answer is kept", async () => {
    const user = userEvent.setup();
    render(<QuestionDock gate={gate(name, runtime)} />);
    await user.type(screen.getByPlaceholderText("Type your answer…"), "draft one");
    await user.click(screen.getByRole("button", { name: "Next question" }));
    expect(screen.getByText("Where will this run?")).toBeDefined();
    await user.click(screen.getByRole("button", { name: "Previous question" }));
    expect((screen.getByPlaceholderText("Type your answer…") as HTMLInputElement).value).toBe(
      "draft one",
    );
    expect(screen.getByRole("button", { name: "Previous question" }).hasAttribute("disabled")).toBe(
      true,
    );
  });

  it("a skipped required question sends nothing and says it is required", async () => {
    const user = userEvent.setup();
    render(<QuestionDock gate={gate(runtime, name)} />);
    await user.click(screen.getByRole("button", { name: "Next question" }));
    await user.type(screen.getByPlaceholderText("Type your answer…"), "Atlas{Enter}");
    expect(post).not.toHaveBeenCalled();
    expect(screen.getByText(/this one's required/)).toBeDefined();
    expect(screen.getByText(/Where will this run\?/)).toBeDefined();
  });

  it("a failed submit says so and the dock stays usable", async () => {
    const user = userEvent.setup();
    const err = vi.spyOn(toast, "error").mockImplementation((() => "") as never);
    post.mockImplementation((() =>
      Promise.reject(new ApiError(409, "conflict", "Already answered."))) as never);
    render(<QuestionDock gate={gate(runtime)} />);
    await user.click(screen.getByRole("button", { name: /Cloud VM/ }));
    await waitFor(() => expect(err).toHaveBeenCalledWith("Already answered."));
    expect(screen.getByRole("button", { name: /Cloud VM/ }).hasAttribute("disabled")).toBe(false);
  });

  it("a multi-select toggles rows and sending with the box empty continues", async () => {
    const user = userEvent.setup();
    render(<QuestionDock gate={gate(tools)} />);
    await user.click(screen.getByRole("button", { name: /Git/ }));
    await user.click(screen.getByRole("button", { name: /Make/ }));
    await user.click(screen.getByRole("button", { name: /Git/ }));
    await user.click(screen.getByRole("button", { name: "Answer and submit" }));
    await waitFor(() => expect(post).toHaveBeenCalled());
    expect(bodyOf()).toEqual({ action: "accept", content: { tools: ["make"] } });
  });

  it("hiding folds the dock to a count, and Answer brings it back", async () => {
    const user = userEvent.setup();
    render(<QuestionDock gate={gate(runtime, name)} />);
    await user.click(screen.getByRole("button", { name: "Hide the questions" }));
    expect(screen.getByText("2 questions waiting")).toBeDefined();
    await user.click(screen.getByRole("button", { name: /2 questions waiting/ }));
    expect(screen.getByText("Where will this run?")).toBeDefined();
  });
});

describe("QuestionDock reply handle", () => {
  function mount(g: GateView) {
    const ref = createRef<QuestionDockHandle>();
    render(<QuestionDock ref={ref} gate={g} />);
    return ref;
  }

  it("a bare number picks that option row", async () => {
    const ref = mount(gate(runtime));
    act(() => ref.current?.answerCurrent("2"));
    await waitFor(() => expect(post).toHaveBeenCalled());
    expect(bodyOf()).toEqual({ action: "accept", content: { runtime: "cloud" } });
  });

  it("a numbered batch answers every question in one send", async () => {
    const ref = mount(gate(runtime, name));
    act(() => ref.current?.answerCurrent("1: kvm; 2: Atlas"));
    await waitFor(() => expect(post).toHaveBeenCalled());
    expect(bodyOf()).toEqual({ action: "accept", content: { runtime: "kvm", name: "Atlas" } });
  });

  it("free text answers the current question and advances", async () => {
    const ref = mount(gate(name, runtime));
    act(() => ref.current?.answerCurrent("Orion"));
    expect(await screen.findByText("Where will this run?")).toBeDefined();
    expect(post).not.toHaveBeenCalled();
  });

  it("a blank reply does nothing", () => {
    const ref = mount(gate(name));
    act(() => ref.current?.answerCurrent("   "));
    expect(post).not.toHaveBeenCalled();
  });
});
