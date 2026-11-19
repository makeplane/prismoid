/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// The composer, driven the way a person drives it: typing, Enter, Shift+Enter, the send button,
// and a send that fails. The draft is never lost on a failed send, and one Enter sends once.
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Composer } from "./composer";

afterEach(cleanup);

const box = () => screen.getByRole("textbox") as HTMLTextAreaElement;

describe("Composer", () => {
  it("Enter sends the trimmed text once and empties the box", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    render(<Composer onSubmit={onSubmit} allowAttachments={false} />);
    await user.type(box(), "  plan the offsite  {Enter}");
    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(onSubmit).toHaveBeenCalledWith({ text: "plan the offsite", attachments: [] });
    await waitFor(() => expect(box().value).toBe(""));
  });

  it("Shift+Enter writes a new line and sends nothing", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    render(<Composer onSubmit={onSubmit} allowAttachments={false} />);
    await user.type(box(), "first{Shift>}{Enter}{/Shift}second");
    expect(onSubmit).not.toHaveBeenCalled();
    expect(box().value).toBe("first\nsecond");
  });

  it("an empty or blank box sends nothing, and its send button is disabled", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    render(<Composer onSubmit={onSubmit} allowAttachments={false} />);
    await user.type(box(), "   {Enter}");
    expect(onSubmit).not.toHaveBeenCalled();
    const send = screen.getAllByRole("button").find((b) => b.getAttribute("type") === "submit");
    expect(send?.hasAttribute("disabled")).toBe(true);
  });

  it("a send that fails keeps the draft, so nothing typed is lost", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn().mockRejectedValue(new Error("offline"));
    render(<Composer onSubmit={onSubmit} allowAttachments={false} />);
    await user.type(box(), "keep me{Enter}");
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    expect(box().value).toBe("keep me");
  });

  it("a second Enter while the first send is in flight sends nothing more", async () => {
    const user = userEvent.setup();
    let settle: () => void = () => {};
    const onSubmit = vi.fn(() => new Promise<void>((resolve) => (settle = resolve)));
    render(<Composer onSubmit={onSubmit} allowAttachments={false} />);
    await user.type(box(), "once{Enter}");
    await user.keyboard("{Enter}");
    expect(onSubmit).toHaveBeenCalledTimes(1);
    settle();
    await waitFor(() => expect(box().value).toBe(""));
  });

  it("the send button sends from the keyboard", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    render(<Composer onSubmit={onSubmit} allowAttachments={false} />);
    await user.type(box(), "by button");
    const send = screen.getAllByRole("button").find((b) => b.getAttribute("type") === "submit");
    expect(send).toBeDefined();
    send!.focus();
    await user.keyboard("{Enter}");
    expect(onSubmit).toHaveBeenCalledWith({ text: "by button", attachments: [] });
  });
});

// The task page mounts the composer with `onStop` while the task runs: the same box steers.
describe("Composer as the steering bar of a live task", () => {
  it("Stop and send stand side by side, and a steer typed mid-run sends, never stops", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    const onStop = vi.fn();
    render(<Composer onSubmit={onSubmit} onStop={onStop} allowAttachments={false} />);
    expect(screen.getByRole("button", { name: "Stop the task" })).toBeDefined();
    await user.type(box(), "use euros{Enter}");
    expect(onSubmit).toHaveBeenCalledWith({ text: "use euros", attachments: [] });
    expect(onStop).not.toHaveBeenCalled();
  });

  it("Stop is one Tab from the box and stops on Enter", async () => {
    const user = userEvent.setup();
    const onStop = vi.fn();
    render(<Composer onSubmit={vi.fn()} onStop={onStop} allowAttachments={false} />);
    await user.click(box());
    await user.tab();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Stop the task" }));
    await user.keyboard("{Enter}");
    expect(onStop).toHaveBeenCalledTimes(1);
  });
});
