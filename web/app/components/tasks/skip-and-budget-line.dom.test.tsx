/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// Skip on a question keeps what was typed (it once declined the whole gate and dropped the
// answers), and the budget card speaks the header's money (formatUsd), not the server's sentence.
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "~/lib/api/client";
import type { Question } from "~/lib/api/events";
import { formatUsd } from "~/lib/format";
import type { GateView } from "~/lib/task-view";
import { BudgetDock } from "./budget-dock";
import { QuestionDock } from "./question-dock";

const recipient: Question = {
  id: "recipient",
  prompt: "Who gets the money?",
  type: "text",
  required: false,
  allow_other: false,
};
const send: Question = {
  id: "send_now",
  prompt: "Send the transfer now?",
  type: "single_select",
  required: false,
  allow_other: false,
  options: [
    { value: "yes", label: "Yes", description: null },
    { value: "no", label: "No", description: null },
  ],
};
const third: Question = {
  id: "note",
  prompt: "A note for the recipient?",
  type: "text",
  required: false,
  allow_other: false,
};

const reply = (data: unknown) =>
  Promise.resolve({ data, error: undefined, response: new Response(null, { status: 200 }) });

let post: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  post = vi
    .spyOn(api, "POST")
    .mockImplementation((() =>
      reply({ resolved_by: "Ada", resolved_by_id: "u1", action: "decline" })) as never);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("Skip on one question", () => {
  it("moves to the next question and sends nothing", async () => {
    const user = userEvent.setup();
    const gate = { gate_id: "g1", questions: [recipient, send, third] } as unknown as GateView;
    render(<QuestionDock gate={gate} />);
    await user.type(screen.getByPlaceholderText("Type your answer…"), "Carol{Enter}");
    expect(screen.getByText("Send the transfer now?")).toBeDefined();
    await user.click(screen.getByRole("button", { name: "Skip" }));
    expect(screen.getByText("A note for the recipient?")).toBeDefined();
    expect(post, `Skip sent ${JSON.stringify(post.mock.calls[0]?.[1])}`).not.toHaveBeenCalled();
  });

  it("Skip on the last question submits what was answered, the skipped ones left out", async () => {
    const user = userEvent.setup();
    const gate = { gate_id: "g1", questions: [recipient, send, third] } as unknown as GateView;
    render(<QuestionDock gate={gate} />);
    await user.type(screen.getByPlaceholderText("Type your answer…"), "Carol{Enter}");
    await user.click(screen.getByRole("button", { name: "Skip" }));
    await user.click(screen.getByRole("button", { name: "Skip" }));
    await waitFor(() => expect(post).toHaveBeenCalled());
    expect((post.mock.calls[0][1] as { body: unknown }).body).toEqual({
      action: "accept",
      content: { recipient: "Carol" },
    });
  });
});

describe("the budget card and the header", () => {
  it("shows the spend in the header's format", () => {
    const spent = 0.008037;
    const gate = {
      gate_id: "g1",
      title: "Budget reached",
      kind: "budget",
      // The reason exactly as gate_budget.open_budget builds it with said_usd.
      reason:
        "the task budget of $0.01 is spent ($0.008037 so far); raise it and resume, or cancel",
      budget_scope: "task",
      budget_figures: { ceiling_usd: "0.01", spent_usd: "0.008037", projected_usd: "0" },
      questions: [],
    } as unknown as GateView;
    const { container } = render(
      <MemoryRouter>
        <BudgetDock gate={gate} taskId="t1" projectId={null} spentUsd={spent} />
      </MemoryRouter>,
    );
    const header = formatUsd(spent); // what TaskChrome's Money shows: "$0.008"
    expect(container.textContent, `header says ${header}`).toContain(`(${header} so far)`);
  });
});
