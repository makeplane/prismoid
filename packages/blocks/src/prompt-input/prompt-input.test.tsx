/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  PromptInput,
  PromptInputButton,
  PromptInputSubmit,
  PromptInputTextarea,
  PromptInputToolbar,
  PromptInputTools,
  type ChatStatus,
} from "./index";

afterEach(cleanup);

function Box({
  onSubmit = vi.fn(),
  status,
  onStop,
  disabled,
}: {
  onSubmit?: (m: { text: string }) => void;
  status?: ChatStatus;
  onStop?: () => void;
  disabled?: boolean;
}) {
  return (
    <PromptInput onSubmit={(m) => onSubmit(m)}>
      <PromptInputTextarea />
      <PromptInputToolbar>
        <PromptInputTools>
          <PromptInputButton aria-label="Attach">+</PromptInputButton>
        </PromptInputTools>
        <PromptInputSubmit status={status} onStop={onStop} disabled={disabled} />
      </PromptInputToolbar>
    </PromptInput>
  );
}

describe("PromptInput", () => {
  it("Enter submits the trimmed message; Shift+Enter writes a new line", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    render(<Box onSubmit={onSubmit} />);
    const box = screen.getByRole("textbox");
    expect(box.getAttribute("placeholder")).toBe("Describe the task…");
    await user.type(box, "line one{Shift>}{Enter}{/Shift}line two");
    expect(onSubmit).not.toHaveBeenCalled();
    await user.type(box, "  {Enter}");
    expect(onSubmit).toHaveBeenCalledWith({ text: "line one\nline two" });
  });

  it("Enter does nothing while the submit button is disabled", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    render(<Box onSubmit={onSubmit} disabled />);
    await user.type(screen.getByRole("textbox"), "hi{Enter}");
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("a tool button is a plain button: it never submits the form", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    render(<Box onSubmit={onSubmit} />);
    const attach = screen.getByRole("button", { name: "Attach" });
    expect(attach.getAttribute("type")).toBe("button");
    await user.click(attach);
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("the submit button reads Submit at rest and Stop while generating, and Stop stops", async () => {
    const user = userEvent.setup();
    const onStop = vi.fn();
    const onSubmit = vi.fn();
    const { rerender } = render(<Box onSubmit={onSubmit} />);
    expect(screen.getByRole("button", { name: "Submit" }).getAttribute("type")).toBe("submit");
    rerender(<Box onSubmit={onSubmit} status="streaming" onStop={onStop} />);
    const stop = screen.getByRole("button", { name: "Stop" });
    expect(stop.getAttribute("type")).toBe("button");
    await user.click(stop);
    expect(onStop).toHaveBeenCalledTimes(1);
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("an icon button's text tooltip is its name; a caller's own name wins", () => {
    render(
      <>
        <PromptInputButton tooltip="Stop the task">
          <svg />
        </PromptInputButton>
        <PromptInputButton tooltip="Dictate" aria-label="Start dictation">
          <svg />
        </PromptInputButton>
      </>,
    );
    expect(screen.getByRole("button", { name: "Stop the task" })).toBeDefined();
    expect(screen.getByRole("button", { name: "Start dictation" })).toBeDefined();
  });

  it("the toolbar is a group", () => {
    render(<Box />);
    // The input group frame and the toolbar both read as groups.
    expect(screen.getAllByRole("group").length).toBeGreaterThanOrEqual(2);
  });
});
