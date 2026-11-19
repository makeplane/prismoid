/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// The Models popover names models and steps the way a person does: the model, the step's
// title - never the provider family or a raw step id - and counts searches apart.

import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { initialView, reduce, type TaskView } from "~/lib/task-view";
import type { TaskEvent } from "~/lib/api/events";
import { ModelsMeter } from "./models-meter";

afterEach(cleanup);

const ev = (frame: Record<string, unknown>) => frame as unknown as TaskEvent;

function viewWithSpend(): TaskView {
  const base = initialView({
    status: "running",
    goal: "g",
    cost: { total_usd: "0" },
    plan: { steps: [] },
  } as unknown as Parameters<typeof initialView>[0]);
  const frames = [
    ev({
      type: "step.state",
      seq: 1,
      step_id: "5f0b8c1e-aaaa",
      title: "Write the memo",
      state: "running",
      attempt: 1,
      agent: "writer",
      detail: "",
    }),
    ev({
      type: "usage.recorded",
      seq: 2,
      step_id: "5f0b8c1e-aaaa",
      model: "claude-sonnet-5-5",
      family: "anthropic",
      cost_usd: "0.08",
      units: { input_token: 5, output_token: 5 },
    }),
    ev({
      type: "usage.recorded",
      seq: 3,
      step_id: "5f0b8c1e-aaaa",
      model: "claude-haiku-5-5",
      family: "anthropic",
      cost_usd: "0.01",
      units: { input_token: 5 },
    }),
    ev({
      type: "usage.recorded",
      seq: 4,
      step_id: "5f0b8c1e-aaaa",
      model: "brave",
      family: "web",
      cost_usd: "0.005",
      units: { search: 1 },
    }),
  ];
  return frames.reduce(reduce, base);
}

describe("ModelsMeter", () => {
  it("lists each model and each step by title, and separates searches from model calls", async () => {
    render(<ModelsMeter view={viewWithSpend()} />);
    await userEvent.click(screen.getByRole("button", { name: /models this task ran/i }));
    expect(await screen.findByText("2 model calls · 1 search")).toBeTruthy();
    expect(screen.getByText("claude-sonnet-5-5")).toBeTruthy();
    expect(screen.getByText("claude-haiku-5-5")).toBeTruthy();
    expect(screen.getByText("Write the memo")).toBeTruthy();
    expect(screen.queryByText("anthropic")).toBeNull();
    expect(screen.queryByText("5f0b8c1e-aaaa")).toBeNull();
  });

  it("says so while nothing has been spent", async () => {
    render(
      <ModelsMeter
        view={initialView({
          status: "created",
          goal: "g",
          cost: { total_usd: "0" },
          plan: { steps: [] },
        } as unknown as Parameters<typeof initialView>[0])}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: /models this task ran/i }));
    expect(await screen.findByText(/No model calls yet/)).toBeTruthy();
  });
});
