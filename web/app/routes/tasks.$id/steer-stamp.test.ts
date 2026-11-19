/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { describe, expect, it } from "vitest";

import { ApiError } from "~/lib/api/client";

import {
  NO_STAMP,
  refusedSteerSurface,
  restamp,
  stampForSend,
  type SteerStamp,
} from "./steer-stamp";

const minter = () => {
  let n = 0;
  return () => `id-${++n}`;
};

describe("the steer's stamp follows the box (ADR-064)", () => {
  it("the first keystroke stamps the epoch and mints one id; typing on keeps both", () => {
    const mint = minter();
    const typed = restamp(NO_STAMP, "use", { refused: null, epoch: 3, mint });
    expect(typed).toEqual({ epoch: 3, commandId: "id-1", sent: null });
    const more = restamp(typed, "use euros", { refused: null, epoch: 4, mint });
    expect(more).toBe(typed);
  });

  it("an emptied box starts over", () => {
    const stamp: SteerStamp = { epoch: 3, commandId: "id-1", sent: "use euros" };
    expect(restamp(stamp, "   ", { refused: "use euros", epoch: 4, mint: minter() })).toEqual(
      NO_STAMP,
    );
  });

  it("a resend of the same words after a lost answer keeps the id - the same row comes back", () => {
    const mint = minter();
    const typed = restamp(NO_STAMP, "use euros", { refused: null, epoch: 3, mint });
    const sent = stampForSend(typed, "use euros", mint);
    expect(sent).toEqual({ epoch: 3, commandId: "id-1", sent: "use euros" });
    expect(restamp(sent, "use euros ", { refused: null, epoch: 3, mint })).toBe(sent);
  });

  it("words edited after a send attempt are a NEW command - never the old row's words", () => {
    const mint = minter();
    const sent = stampForSend(
      restamp(NO_STAMP, "use euros", { refused: null, epoch: 3, mint }),
      "use euros",
      mint,
    );
    const edited = restamp(sent, "use pounds", { refused: null, epoch: 5, mint });
    expect(edited.commandId).toBe("id-2");
    expect(edited.commandId).not.toBe(sent.commandId);
    expect(edited.epoch).toBe(3); // the typing epoch stands: only the strip re-stamps it
    expect(edited.sent).toBeNull();
    // and typing on from there keeps the new id
    expect(restamp(edited, "use pounds now", { refused: null, epoch: 5, mint })).toBe(edited);
  });

  it("refused words, once edited, are a fresh draft against the epoch shown now", () => {
    const mint = minter();
    const sent: SteerStamp = { epoch: 2, commandId: "id-0", sent: "name it tasks.py" };
    expect(restamp(sent, "name it tasks.py", { refused: "name it tasks.py", epoch: 4, mint })).toBe(
      sent,
    );
    expect(
      restamp(sent, "name it todo.py", { refused: "name it tasks.py", epoch: 4, mint }),
    ).toEqual({
      epoch: 4,
      commandId: "id-1",
      sent: null,
    });
  });

  it("a send with no stamp yet mints its id there", () => {
    expect(stampForSend(NO_STAMP, "go", () => "fresh")).toEqual({
      epoch: null,
      commandId: "fresh",
      sent: "go",
    });
  });
});

describe("where a refused steer says so", () => {
  const stale = new ApiError(409, "stale_steer", "Bob steered while you were typing: 'use euros'");

  it("a stale steer whose words are in the box draws the strip", () => {
    expect(refusedSteerSurface(stale, "use pounds", " use pounds ")).toBe("strip");
  });

  it("a stale 'Send as steer' whose words left the box is a toast, never nothing", () => {
    expect(refusedSteerSurface(stale, "use pounds", "something else")).toBe("toast");
    expect(refusedSteerSurface(stale, "use pounds", "")).toBe("toast");
  });

  it("any other refusal is a toast", () => {
    expect(refusedSteerSurface(new ApiError(403, "forbidden", "viewers watch"), "x", "x")).toBe(
      "toast",
    );
    expect(refusedSteerSurface(new Error("network"), "x", "x")).toBe("toast");
  });
});
