/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { MachineChip, chipBehindTheRun } from "./machine-chip";

const base = {
  state: "asleep",
  profile: "browser",
  state_since: "2026-09-30T14:02:00Z",
  last_active_at: "2026-09-30T13:52:00Z",
  checkpoint_at: null,
  checkpoint_bytes: null,
  browser_saved: true,
  warm_seconds: 2280,
  cost_usd: "2.85",
} as const;

describe("MachineChip", () => {
  it("shows the state, the profile, the machine time and its cost in dollars", () => {
    const html = renderToStaticMarkup(
      <MachineChip machine={base} canEdit={false} onSleep={() => {}} onReset={() => {}} />,
    );
    expect(html).toContain("Asleep");
    expect(html).toContain("browser");
    expect(html).toContain("38 min");
    expect(html).toContain("$2.85");
  });

  it("gives editors the buttons and viewers none", () => {
    const viewer = renderToStaticMarkup(
      <MachineChip
        machine={{ ...base, state: "idle" }}
        canEdit={false}
        onSleep={() => {}}
        onReset={() => {}}
      />,
    );
    expect(viewer).not.toContain("Sleep now");
    const editor = renderToStaticMarkup(
      <MachineChip
        machine={{ ...base, state: "idle" }}
        canEdit
        onSleep={() => {}}
        onReset={() => {}}
      />,
    );
    expect(editor).toContain("Sleep now");
    expect(editor).toContain("Reset");
  });

  it("disables Sleep now while it works", () => {
    const html = renderToStaticMarkup(
      <MachineChip
        machine={{ ...base, state: "running" }}
        canEdit
        onSleep={() => {}}
        onReset={() => {}}
      />,
    );
    expect(html).toMatch(/<button[^>]*disabled[^>]*>(?:<span[^>]*>)?\s*Sleep now/);
    expect(html).toMatch(/<button[^>]*disabled[^>]*>(?:<span[^>]*>)?\s*Reset/);
    expect(html).toContain("aria-describedby");
    expect(html).toContain("It&#x27;s working");
  });

  it("leads with State, profile and then since", () => {
    const html = renderToStaticMarkup(
      <MachineChip
        machine={{ ...base, state: "running", profile: "base" }}
        canEdit={false}
        onSleep={() => {}}
        onReset={() => {}}
      />,
    );
    expect(html).toContain("Running");
    expect(html).toMatch(/Running[\s\S]*·[\s\S]*base/);
    expect(html).toContain("since");
  });
});

describe("chipBehindTheRun - a resumed step re-binds with no machine.* frame", () => {
  it("a parked chip on a running task with nothing open is behind: the page re-reads it", () => {
    expect(chipBehindTheRun({ state: "parked" }, "running", 0)).toBe(true);
  });

  it("is not behind while the task is paused or waits on a gate, or once it reads running", () => {
    expect(chipBehindTheRun({ state: "parked" }, "paused", 0)).toBe(false);
    expect(chipBehindTheRun({ state: "parked" }, "waiting", 1)).toBe(false);
    expect(chipBehindTheRun({ state: "parked" }, "running", 1)).toBe(false);
    expect(chipBehindTheRun({ state: "running" }, "running", 0)).toBe(false);
    expect(chipBehindTheRun(null, "running", 0)).toBe(false);
  });
});

describe("MachineChip under a control lease (ADR-065)", () => {
  const lease = (untilMs: number, name = "Bob") => ({
    holder: { id: "bob", name },
    private: true,
    since: new Date(untilMs - 60_000).toISOString(),
    until: new Date(untilMs).toISOString(),
  });

  it("disables Sleep now and Reset while someone drives, and names them", () => {
    const html = renderToStaticMarkup(
      <MachineChip
        machine={{ ...base, state: "idle", control: lease(Date.now() + 60_000) }}
        canEdit
        onSleep={() => {}}
        onReset={() => {}}
      />,
    );
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*>(?:<span[^>]*>)?\s*Sleep now/);
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*>(?:<span[^>]*>)?\s*Reset/);
    expect(html).toContain("Bob is driving the browser.");
  });

  it("an unnamed holder is someone", () => {
    const html = renderToStaticMarkup(
      <MachineChip
        machine={{ ...base, state: "idle", control: lease(Date.now() + 60_000, "") }}
        canEdit
        onSleep={() => {}}
        onReset={() => {}}
      />,
    );
    expect(html).toContain("Someone is driving the browser.");
  });

  it("never expires a lease on its own clock, and no lease frees the buttons", () => {
    const past = renderToStaticMarkup(
      <MachineChip
        machine={{ ...base, state: "idle", control: lease(Date.now() - 1) }}
        canEdit
        onSleep={() => {}}
        onReset={() => {}}
      />,
    );
    expect(past).toContain("Bob is driving the browser.");
    const none = renderToStaticMarkup(
      <MachineChip
        machine={{ ...base, state: "idle", control: null }}
        canEdit
        onSleep={() => {}}
        onReset={() => {}}
      />,
    );
    expect(none).not.toMatch(/<button[^>]*disabled=""[^>]*>(?:<span[^>]*>)?\s*Sleep now/);
    expect(none).not.toContain("is driving the browser");
  });
});
