/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { describe, expect, it } from "vitest";
import { fireNote } from "./fire-note";

describe("fireNote", () => {
  it("a mint fired", () => {
    expect(fireNote({ decision: "minted", detail: "" })).toEqual({
      tone: "success",
      text: "Fired - the run shows below.",
    });
  });

  it("a hold made a task that waits for its release", () => {
    expect(fireNote({ decision: "held", detail: "" }).tone).toBe("success");
  });

  it("a stand-down says nothing fired, and why", () => {
    expect(fireNote({ decision: "skipped_season", detail: "max_runs reached" })).toEqual({
      tone: "info",
      text: "Not fired (skipped season) - the run shows below.",
    });
    expect(fireNote({ decision: "superseded", detail: "" }).text).toMatch(/^Not fired/);
  });

  it("an error names its detail", () => {
    expect(fireNote({ decision: "error", detail: "no budget" })).toEqual({
      tone: "error",
      text: "The fire failed: no budget",
    });
  });
});
