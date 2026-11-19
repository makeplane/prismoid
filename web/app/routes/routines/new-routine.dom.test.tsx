/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// Creating a routine from its dialog: Save stays off until name, instruction and a When are all
// present, each When lane (preset, cron, interval, events) reaches the wire in its own shape,
// a failed save keeps the form, and a good one closes it and reports the new id.
import { toast } from "@prismoid/blocks/toast";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "~/lib/api/client";
import { NewRoutine } from "./new-routine";

const reply = (status: number, data: unknown, error?: unknown) =>
  Promise.resolve({ data, error, response: new Response(null, { status }) });

let post: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  post = vi
    .spyOn(api, "POST")
    .mockImplementation((() => reply(200, { id: "r-new", upcoming: [] })) as never);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  document.body.innerHTML = "";
});

const body = () => (post.mock.calls[0]![1] as { body: Record<string, unknown> }).body;
const save = () => screen.getByRole("button", { name: "Save routine" });

async function openAndFill(user: ReturnType<typeof userEvent.setup>, onCreated = vi.fn()) {
  render(<NewRoutine onCreated={onCreated} />);
  await user.click(screen.getByRole("button", { name: "New routine" }));
  await user.type(await screen.findByRole("textbox", { name: "Name" }), "  daily-brief ");
  await user.type(screen.getByRole("textbox", { name: "Instruction" }), " Summarise the news ");
  return onCreated;
}

describe("NewRoutine", () => {
  it("Save is disabled until a When is picked, and the footer says so", async () => {
    const user = userEvent.setup();
    await openAndFill(user);
    expect(save().hasAttribute("disabled")).toBe(true);
    expect(screen.getByText("Pick a When - a routine needs one.")).toBeDefined();
    await user.click(screen.getByRole("button", { name: "Every hour" }));
    expect(save().hasAttribute("disabled")).toBe(false);
    expect(screen.getByText("cron 0 * * * *")).toBeDefined();
  });

  it("a preset, a budget and trimmed text go out as the create body, then the id is reported", async () => {
    const user = userEvent.setup();
    const success = vi.spyOn(toast, "success");
    post.mockImplementation((() =>
      reply(200, { id: "r-9", upcoming: ["2026-11-02T09:00:00Z"] })) as never);
    const onCreated = await openAndFill(user);
    await user.click(screen.getByRole("button", { name: "Mondays, 9:00" }));
    await user.type(screen.getByRole("textbox", { name: "Budget per run" }), "2.5");
    await user.click(save());
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith("r-9"));
    expect(post.mock.calls[0]?.[0]).toBe("/v1/routines");
    expect(body()).toEqual({
      name: "daily-brief",
      instruction: "Summarise the news",
      schedule: { cron: "0 9 * * 1", every: null },
      budget_usd: 2.5,
    });
    expect(String(success.mock.calls[0]?.[0])).toContain("first fire");
    await waitFor(() => expect(screen.queryByRole("textbox", { name: "Name" })).toBeNull());
  });

  it("typing a cron and then an interval swaps the lane and drops the other draft", async () => {
    const user = userEvent.setup();
    await openAndFill(user);
    const cron = screen.getByPlaceholderText('cron, e.g. "30 8 * * 1-5"') as HTMLInputElement;
    await user.type(cron, "5 4 * * *");
    expect(screen.getByText("cron 5 4 * * *")).toBeDefined();
    await user.type(screen.getByPlaceholderText('every, e.g. "4h"'), "4h");
    expect(screen.getByText("every 4h")).toBeDefined();
    expect(cron.value).toBe("");
    await user.click(save());
    await waitFor(() => expect(post).toHaveBeenCalled());
    expect(body().schedule).toEqual({ cron: null, every: "4h" });
    expect(body()).not.toHaveProperty("budget_usd");
  });

  it("event kinds toggle on and off, and send on_events with the held note shown", async () => {
    const user = userEvent.setup();
    await openAndFill(user);
    await user.click(screen.getByRole("button", { name: "a task fails" }));
    await user.click(screen.getByRole("button", { name: /a task delivers/ }));
    expect(screen.getByText(/Event routines start held/)).toBeDefined();
    await user.click(screen.getByRole("button", { name: /a task delivers/ }));
    await user.click(save());
    await waitFor(() => expect(post).toHaveBeenCalled());
    expect(body().on_events).toEqual(["task.failed"]);
    expect(body()).not.toHaveProperty("schedule");
  });

  it("untoggling the last event kind leaves no When", async () => {
    const user = userEvent.setup();
    await openAndFill(user);
    await user.click(screen.getByRole("button", { name: "a task fails" }));
    expect(save().hasAttribute("disabled")).toBe(false);
    await user.click(screen.getByRole("button", { name: /a task fails/ }));
    expect(save().hasAttribute("disabled")).toBe(true);
  });

  it("a failed save toasts the server's words and keeps the dialog and its text", async () => {
    const user = userEvent.setup();
    const error = vi.spyOn(toast, "error");
    post.mockImplementation((() =>
      reply(422, undefined, {
        error: { code: "bad", message: "That cron is invalid." },
      })) as never);
    const onCreated = await openAndFill(user);
    await user.click(screen.getByRole("button", { name: "Every day, 9:00" }));
    await user.click(save());
    await waitFor(() => expect(error).toHaveBeenCalledWith("That cron is invalid."));
    expect(onCreated).not.toHaveBeenCalled();
    expect((screen.getByRole("textbox", { name: "Name" }) as HTMLInputElement).value).toContain(
      "daily-brief",
    );
    expect(save().hasAttribute("disabled")).toBe(false);
  });
});
