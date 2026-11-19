/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// The stale-steer strip, driven: a refused steer is announced, and both ways on (Send anyway,
// Discard) work from the keyboard; while one is in flight neither can be pressed again.
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { StaleSteerStrip, type StaleSteer } from "./stale-steer-strip";

afterEach(cleanup);

const stale: StaleSteer = {
  text: "make it shorter",
  missed: [{ kind: "steer", author: "Bob", text: "use euros", epoch: 3, state: "accepted" }],
};

describe("StaleSteerStrip", () => {
  it("is announced as an alert that names what was missed", () => {
    render(<StaleSteerStrip stale={stale} onSendAnyway={vi.fn()} onDiscard={vi.fn()} />);
    expect(screen.getByRole("alert").textContent).toContain(
      "Bob steered while you were typing: 'use euros'",
    );
  });

  it("Tab reaches Discard then Send anyway, and Enter or Space presses each", async () => {
    const user = userEvent.setup();
    const onSendAnyway = vi.fn();
    const onDiscard = vi.fn();
    render(<StaleSteerStrip stale={stale} onSendAnyway={onSendAnyway} onDiscard={onDiscard} />);
    await user.tab();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Discard" }));
    await user.keyboard("{Enter}");
    expect(onDiscard).toHaveBeenCalledTimes(1);
    await user.tab();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Send anyway" }));
    await user.keyboard(" ");
    expect(onSendAnyway).toHaveBeenCalledTimes(1);
  });

  it("while busy, neither button can be pressed", async () => {
    const user = userEvent.setup();
    const onSendAnyway = vi.fn();
    const onDiscard = vi.fn();
    render(
      <StaleSteerStrip stale={stale} busy onSendAnyway={onSendAnyway} onDiscard={onDiscard} />,
    );
    await user.click(screen.getByRole("button", { name: "Send anyway" }));
    await user.click(screen.getByRole("button", { name: "Discard" }));
    expect(onSendAnyway).not.toHaveBeenCalled();
    expect(onDiscard).not.toHaveBeenCalled();
  });
});
