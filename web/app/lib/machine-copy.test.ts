/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { describe, expect, it } from "vitest";

import { heldText, machineMarkerText, machineWord, minutes } from "./machine-copy";

describe("machine copy", () => {
  it("names every state in the chip's five words", () => {
    expect(
      ["waking", "running", "parked", "idle", "sleeping", "asleep", "gone"].map(machineWord),
    ).toEqual(["Running", "Running", "Waiting", "Idle", "Asleep", "Asleep", "Gone"]);
  });

  it("says why it slept, and who put it to sleep", () => {
    expect(
      machineMarkerText({ type: "machine.slept", reason: "idle", after_s: 600 } as never),
    ).toBe("Machine slept after 10 min unused");
    expect(
      machineMarkerText({
        type: "machine.slept",
        reason: "manual",
        actor: { name: "Bob" },
      } as never),
    ).toBe("Bob put the Machine to sleep");
    expect(
      machineMarkerText({
        type: "machine.slept",
        reason: "idle",
        dropped_bytes: 3.1 * 1024 ** 3,
      } as never),
    ).toBe("Machine slept: 3.1 GB of files were over the 2 GB limit and were not kept");
  });

  it("names the turn author on a wake, and rebuilds and resets plainly", () => {
    expect(
      machineMarkerText({ type: "machine.woke", turn: 3, actor: { name: "Ada" } } as never, {
        turn: 2,
      }),
    ).toBe("Machine woke for Ada's follow-up");
    expect(
      machineMarkerText({ type: "machine.woke", turn: 2, browser_restored: false } as never),
    ).toContain("browser logins could not be restored");
    expect(
      machineMarkerText({
        type: "machine.rebuilt",
        reason: "expired",
        retention_days: 14,
      } as never),
    ).toBe("Machine rebuilt: its saved files expired after 14 days");
    expect(machineMarkerText({ type: "machine.reset", actor: { name: "Ada" } } as never)).toBe(
      "Ada reset the Machine",
    );
    // created = the row was made, never "started": a frame may not have booted (or ever boot).
    expect(machineMarkerText({ type: "machine.created", profile: "base" } as never)).toBe(
      "Machine created",
    );
    expect(
      machineMarkerText({
        type: "machine.created",
        profile: "base",
        seeded_as_of: "2026-01-01T00:00:00Z",
      } as never),
    ).toBe("Machine created from the parent's files");
  });

  it("says what a lost or oversize rebuild lost, never that saved files failed to restore", () => {
    // `lost` is said when a frame died (the work since the last save is gone) and when a
    // checkpoint vanished; either way some files are gone - never "could not be restored".
    expect(machineMarkerText({ type: "machine.rebuilt", reason: "lost" } as never)).toBe(
      "Machine lost some of its files",
    );
    expect(machineMarkerText({ type: "machine.rebuilt", reason: "oversize" } as never)).toBe(
      "Machine rebuilt: its files were too large to save",
    );
  });

  it("does not claim a follow-up on a park resume or an upgrade wake", () => {
    expect(
      machineMarkerText({
        type: "machine.woke",
        turn: 2,
        from_park: true,
        actor: { name: "Ada" },
      } as never),
    ).toBe("Machine woke to continue the step");
    expect(
      machineMarkerText({ type: "machine.woke", turn: 2, profile: "browser" } as never, {
        profile: "base",
      }),
    ).toBe("Machine woke on the browser image");
  });

  it("calls a wake a follow-up only when it is a later turn than the Machine's last", () => {
    const woke = {
      type: "machine.woke",
      turn: 3,
      profile: "base",
      actor: { name: "Ada" },
    } as never;
    expect(machineMarkerText(woke, { profile: "base", turn: 2 })).toBe(
      "Machine woke for Ada's follow-up",
    );
    // the same turn's wake (a retry, a resumed step) is nobody's follow-up, whoever's turn it is
    expect(machineMarkerText(woke, { profile: "base", turn: 3 })).toBe("Machine woke");
    // no earlier Machine event to compare with: claim nothing
    expect(machineMarkerText(woke)).toBe("Machine woke");
  });

  it("words a sleep only from what the event says", () => {
    expect(
      machineMarkerText({ type: "machine.slept", reason: "idle", after_s: 660 } as never),
    ).toBe("Machine slept after 11 min unused");
    // a reason this build does not know is not guessed to be idleness
    expect(
      machineMarkerText({ type: "machine.slept", reason: "reaped", after_s: 660 } as never),
    ).toBe("Machine slept");
  });

  it("rounds minutes", () => {
    expect(minutes(2280)).toBe(38);
  });

  it("says who took control of the browser, and whether privately", () => {
    expect(
      machineMarkerText({
        type: "machine.control_claimed",
        private: true,
        actor: { name: "Bob" },
      } as never),
    ).toBe("Bob took control of the browser (private)");
    expect(
      machineMarkerText({ type: "machine.control_claimed", actor: { name: "Ada" } } as never),
    ).toBe("Ada took control of the browser");
  });

  it("says how control ended, naming the owner who ended it", () => {
    const bob = { name: "Bob" };
    expect(
      machineMarkerText({
        type: "machine.control_ended",
        reason: "released",
        held_s: 100,
        actor: bob,
      } as never),
    ).toBe("Bob handed back control after 1m 40s");
    expect(
      machineMarkerText({
        type: "machine.control_ended",
        reason: "expired",
        held_s: 75,
        actor: { name: "Ada" },
      } as never),
    ).toBe("Ada's control expired after a minute with no input");
    expect(
      machineMarkerText({
        type: "machine.control_ended",
        reason: "ended",
        held_s: 30,
        actor: bob,
        ended_by: { name: "Ada" },
      } as never),
    ).toBe("Ada ended Bob's control");
    expect(
      machineMarkerText({ type: "machine.control_ended", reason: "lost", actor: bob } as never),
    ).toBe("Bob's control ended: the Machine lost its browser");
  });

  it("words held time the way the agent's resume text does", () => {
    expect([heldText(45), heldText(100), heldText(7380)]).toEqual(["45s", "1m 40s", "2h 3m"]);
  });
});
