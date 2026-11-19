/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { describe, expect, it } from "vitest";
import {
  BAR_FLOOR,
  appendHeard,
  barLevels,
  editOverHeard,
  readResults,
  voiceErrorMessage,
} from "./voice";

const result = (transcript: string, isFinal?: boolean) =>
  Object.assign([{ transcript }], isFinal === undefined ? {} : { isFinal });

describe("readResults", () => {
  it("separates settled words from words still being heard", () => {
    const got = readResults(0, [result(" buy milk ", true), result(" and eg", false)]);
    expect(got).toEqual({ settled: "buy milk", hearing: "and eg" });
  });

  it("treats a result with no flag as settled, and starts at the event's index", () => {
    expect(readResults(1, [result("old"), result("new")])).toEqual({ settled: "new", hearing: "" });
  });
});

describe("voiceErrorMessage", () => {
  it("says nothing when the person stopped or stayed silent", () => {
    expect(voiceErrorMessage("aborted")).toBeNull();
    expect(voiceErrorMessage("no-speech")).toBeNull();
  });

  it("names the cause for a blocked microphone and an unreachable service", () => {
    expect(voiceErrorMessage("not-allowed")).toMatch(/blocked/);
    expect(voiceErrorMessage("network")).toMatch(/speech service/);
    expect(voiceErrorMessage(undefined)).toMatch(/unexpectedly/);
  });
});

describe("barLevels", () => {
  it("draws silence at the floor and loud speech at full height", () => {
    expect(barLevels(new Uint8Array(32), 4)).toEqual([BAR_FLOOR, BAR_FLOOR, BAR_FLOOR, BAR_FLOOR]);
    expect(barLevels(new Uint8Array(32).fill(255), 4)).toEqual([1, 1, 1, 1]);
  });

  it("gives each bar its own band", () => {
    const spectrum = new Uint8Array(30);
    spectrum.fill(180, 0, 5); // the first of four bands across the usable 20 bins
    expect(barLevels(spectrum, 4)).toEqual([1, BAR_FLOOR, BAR_FLOOR, BAR_FLOOR]);
  });
});

describe("heard words in the box", () => {
  it("appendHeard joins with one space, and adds nothing for silence", () => {
    expect(appendHeard("", "buy milk")).toBe("buy milk");
    expect(appendHeard("Note: ", "buy milk")).toBe("Note: buy milk");
    expect(appendHeard("typed", "")).toBe("typed");
  });

  it("typing ahead of the heard words leaves them the recogniser's", () => {
    const shown = appendHeard("Note:", "buy mi");
    expect(editOverHeard("My " + shown, "buy mi")).toEqual({ text: "My Note: ", adopted: false });
  });

  it("an edit that touches the heard words adopts them, so they are never delivered twice", () => {
    expect(editOverHeard("buy milk!", "buy milk")).toEqual({ text: "buy milk!", adopted: true });
    expect(editOverHeard("buy mil", "buy milk")).toEqual({ text: "buy mil", adopted: true });
  });

  it("with nothing being heard, an edit is just an edit", () => {
    expect(editOverHeard("typed", "")).toEqual({ text: "typed", adopted: false });
  });
});
