/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/** The control dock (ADR-065): a CONTROL gate's one line, and its two verbs. */
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({ post: vi.fn() }));
vi.mock("~/lib/api/client", async (orig) => ({
  ...(await orig<typeof import("~/lib/api/client")>()),
  api: { POST: state.post },
  unwrap: async (p: Promise<{ err?: Error }>) => {
    const v = await p;
    if (v?.err) throw v.err;
    return v;
  },
}));

import type { GateView } from "~/lib/task-view";
import { ControlDock, controlLine, doneHandoff, skipHandoff } from "./control-dock";

const gate = (over: Partial<GateView> = {}): GateView => ({
  gate_id: "g1",
  kind: "control",
  source: "handoff",
  title: "The agent needs someone to sign in to admin.acme.io",
  reason: "sign in to admin.acme.io",
  questions: [],
  ...over,
});

const html = (g: GateView, canEdit = true) =>
  renderToStaticMarkup(<ControlDock gate={g} canEdit={canEdit} onOpenScreen={() => {}} />);

beforeEach(() => state.post.mockReset());

describe("the control dock", () => {
  it("a handoff: the line, Open the screen, and Skip for an editor", () => {
    const out = html(gate());
    expect(out).toContain("The agent needs someone to sign in to admin.acme.io");
    expect(out).toContain(">Open the screen<");
    expect(out).toContain(">Skip<");
    expect(out).not.toContain(">Allow<"); // nobody accepts a CONTROL gate by hand
  });

  it("a viewer opens the screen but cannot Skip", () => {
    const out = html(gate(), false);
    expect(out).toContain(">Open the screen<");
    expect(out).not.toContain(">Skip<");
  });

  it("a takeover has nothing to skip: the hand-back answers it", () => {
    const out = html(gate({ source: "takeover", title: "Ada took control of the browser" }));
    expect(out).toContain("Ada took control of the browser");
    expect(out).not.toContain(">Skip<");
  });

  it("the line falls back to the reason, then to plain words", () => {
    expect(controlLine({ title: " ", reason: "sign in to x", source: "handoff" })).toBe(
      "sign in to x",
    );
    expect(controlLine({ source: "takeover" })).toBe("Someone took control of the browser");
  });
});

describe("Skip", () => {
  it("declines the gate through the ordinary resolve route", async () => {
    state.post.mockResolvedValue({ status: "resolved" });
    await skipHandoff("g1");
    expect(state.post).toHaveBeenCalledWith("/v1/gates/{gate_id}/resolve", {
      params: { path: { gate_id: "g1" } },
      body: { action: "decline" },
    });
  });
});

describe("ControlGateCard", () => {
  // A device Machine: the browser is a window on the creator's own computer - no screen.
  const device = (isCreator: boolean, deviceName: string | null = "MacBook Pro") =>
    renderToStaticMarkup(
      <ControlDock
        gate={gate({ machine_driver: "device" })}
        canEdit
        isCreator={isCreator}
        deviceName={deviceName}
        onOpenScreen={() => {}}
      />,
    );

  it("shows Done for device", () => {
    const out = device(true);
    expect(out).toContain(">Done<");
    expect(out).toContain("Browser on MacBook Pro");
    expect(out).not.toContain("Open the screen"); // a device Machine has no screen to open
    expect(out).toContain(">Skip<");
  });

  it("offers Done to the creator only, and names your computer when the device has no name", () => {
    const out = device(false, null);
    expect(out).not.toContain(">Done<");
    expect(out).not.toContain("Open the screen");
    expect(out).toContain("Browser on your computer");
  });

  it("a cloud gate's line keeps its markup", () => {
    expect(html(gate())).toContain(
      '<p class="min-w-0 flex-1 text-body-sm-regular break-words">The agent needs someone to sign in to admin.acme.io</p>',
    );
  });

  it("a cloud gate never offers Done", () => {
    const out = renderToStaticMarkup(
      <ControlDock gate={gate()} canEdit isCreator onOpenScreen={() => {}} />,
    );
    expect(out).not.toContain(">Done<");
    expect(out).toContain(">Open the screen<");
  });

  it("Done accepts the gate through the ordinary resolve route", async () => {
    state.post.mockResolvedValue({ status: "resolved" });
    await doneHandoff("g1");
    expect(state.post).toHaveBeenCalledWith("/v1/gates/{gate_id}/resolve", {
      params: { path: { gate_id: "g1" } },
      body: { action: "accept" },
    });
  });
});
