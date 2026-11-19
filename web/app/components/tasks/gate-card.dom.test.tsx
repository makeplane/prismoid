/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// The gate card resolved the way a person resolves it: answer and submit, decline, cancel,
// pick an approval's scope with the keyboard. What reaches the wire is the resolve body, and a
// failed resolve hands the buttons back.
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "~/lib/api/client";
import type { GateView } from "~/lib/task-view";
import { GateCard } from "./gate-card";

const ok = (data: unknown) =>
  Promise.resolve({ data, error: undefined, response: new Response(null, { status: 200 }) });

let post: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  post = vi.spyOn(api, "POST").mockImplementation((() => ok({ action: "accept" })) as never);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

/** The body of the n-th resolve call. */
const body = (n = 0) => (post.mock.calls[n]?.[1] as { body: unknown } | undefined)?.body;

const questionGate: GateView = {
  gate_id: "g1",
  kind: "input",
  title: "Two quick questions",
  questions: [
    { id: "audience", type: "text", prompt: "Who reads it?", required: true, allow_other: false },
    { id: "pages", type: "number", prompt: "How many pages?", required: false, allow_other: false },
  ],
};

describe("GateCard, a question gate", () => {
  it("a required answer left blank says so and sends nothing", async () => {
    const user = userEvent.setup();
    render(<GateCard gate={questionGate} />);
    await user.click(screen.getByRole("button", { name: "Submit answers" }));
    expect(screen.getByText("This one's required.")).toBeDefined();
    expect(post).not.toHaveBeenCalled();
  });

  it("typed answers go out as the accept's content, then the card reports resolved", async () => {
    const user = userEvent.setup();
    const onResolved = vi.fn();
    render(<GateCard gate={questionGate} onResolved={onResolved} />);
    await user.type(screen.getByPlaceholderText("Your answer…"), "  the board  ");
    await user.click(screen.getByRole("button", { name: "Submit answers" }));
    await waitFor(() => expect(onResolved).toHaveBeenCalledTimes(1));
    expect(post.mock.calls[0]?.[0]).toBe("/v1/gates/{gate_id}/resolve");
    expect(body()).toEqual({ action: "accept", content: { audience: "the board" } });
  });

  it("Decline pressed from the keyboard sends a decline and no content", async () => {
    const user = userEvent.setup();
    render(<GateCard gate={questionGate} />);
    const decline = screen.getByRole("button", { name: "Decline · proceed anyway" });
    decline.focus();
    await user.keyboard("{Enter}");
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    expect(body()).toEqual({ action: "decline" });
  });

  it("a failed resolve keeps the card and hands the buttons back", async () => {
    post.mockImplementation((() =>
      Promise.resolve({
        data: undefined,
        error: { error: { code: "conflict", message: "Already answered" } },
        response: new Response(null, { status: 409 }),
      })) as never);
    const user = userEvent.setup();
    const onResolved = vi.fn();
    render(<GateCard gate={questionGate} onResolved={onResolved} />);
    const cancel = screen.getByRole("button", { name: "Cancel task" });
    await user.click(cancel);
    await waitFor(() => expect(cancel.hasAttribute("disabled")).toBe(false));
    expect(body()).toEqual({ action: "cancel" });
    expect(onResolved).not.toHaveBeenCalled();
  });
});

describe("GateCard, an approval with scopes", () => {
  const approve: GateView = {
    gate_id: "g2",
    kind: "approve",
    title: "Allow the browser to open sso.vendor.com?",
    reason: "sso.vendor.com: browser.open",
    scopes: ["once", "task", "always"],
    host: "sso.vendor.com",
    questions: [],
  };

  it("Allow sends the default scope, For this task", async () => {
    const user = userEvent.setup();
    render(<GateCard gate={approve} />);
    await user.click(screen.getByRole("button", { name: "Allow" }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    expect(body()).toEqual({ action: "accept", content: { scope: "task" } });
  });

  it("the arrow keys move the scope, and Allow sends the one chosen", async () => {
    const user = userEvent.setup();
    render(<GateCard gate={approve} />);
    screen.getByRole("radio", { name: "For this task" }).focus();
    await user.keyboard("{ArrowUp}");
    expect(screen.getByRole("radio", { name: "Just this once" }).getAttribute("aria-checked")).toBe(
      "true",
    );
    await user.click(screen.getByRole("button", { name: "Allow" }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    expect(body()).toEqual({ action: "accept", content: { scope: "once" } });
  });

  it("Deny continues without the action", async () => {
    const user = userEvent.setup();
    render(<GateCard gate={approve} />);
    await user.click(screen.getByRole("button", { name: "Deny · continue without" }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    expect(body()).toEqual({ action: "decline" });
  });
});

describe("GateCard, a budget gate with no task in hand", () => {
  const budgetGate = (scope: string): GateView => ({
    gate_id: "g9",
    kind: "budget",
    title: scope === "workspace" ? "Spending allowance reached" : "Budget reached",
    reason: "the reason",
    questions: [],
    budget_scope: scope,
  });

  it("a Spending allowance reached gate offers only Cancel task: the allowance resumes it, not a person", () => {
    render(<GateCard gate={budgetGate("workspace")} />);
    expect(screen.getByText("the reason")).toBeDefined();
    expect(screen.queryByRole("button", { name: "Resume" })).toBeNull();
    expect(screen.getByRole("button", { name: "Cancel task" })).toBeDefined();
  });

  it("a Budget reached gate still offers Resume", () => {
    render(<GateCard gate={budgetGate("task")} />);
    expect(screen.getByRole("button", { name: "Resume" })).toBeDefined();
  });
});
