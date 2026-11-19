/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { describe, expect, it } from "vitest";
import { workingPhrase } from "./status-copy";
import type { StepView } from "~/lib/task-view";

const step = (partial: Partial<StepView> & Pick<StepView, "id" | "title" | "state">): StepView => ({
  attempt: 0,
  ...partial,
});

describe("workingPhrase", () => {
  it("uses phase verbs before any plan", () => {
    expect(workingPhrase("clarifying", [])).toBe("deciding what to ask…");
    expect(workingPhrase("planning", [])).toBe("planning the approach…");
  });

  it("names the running step", () => {
    expect(
      workingPhrase("running", [step({ id: "1", title: "Draft the memo", state: "running" })]),
    ).toBe("Draft the memo…");
  });

  it("prefers verifying / retrying copy", () => {
    expect(workingPhrase("running", [step({ id: "1", title: "Draft", state: "verifying" })])).toBe(
      "verifying Draft…",
    );
    expect(workingPhrase("running", [step({ id: "1", title: "Draft", state: "retrying" })])).toBe(
      "retrying Draft…",
    );
  });

  it("skips empty titles", () => {
    expect(workingPhrase("running", [step({ id: "1", title: "  ", state: "running" })])).toBe(
      "working…",
    );
  });

  it("waiting is for the human", () => {
    expect(workingPhrase("waiting", [])).toBe("waiting for you…");
  });
});
