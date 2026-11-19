/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { describe, expect, it } from "vitest";
import type { FeedItem } from "~/lib/task-view";
import { epilogueVerdict } from "./epilogue-verdict";

type Item = Extract<FeedItem, { kind: "epilogue" }>;
const item = (over: Partial<Item> = {}): Item => ({
  kind: "epilogue",
  id: "ep1",
  delivered: true,
  remembered: [],
  ...over,
});

describe("epilogueVerdict", () => {
  it("a failed receipt is an amber caveat", () => {
    expect(epilogueVerdict(item({ verdict: "fail" }))).toMatchObject({
      tone: "warning",
      caveat: true,
      text: "Checks found problems. Read it with care.",
    });
  });

  it("a clean receipt is green and names its suite and judge", () => {
    const v = epilogueVerdict(item({ verdict: "pass", suite: "document-suite", family: "google" }));
    expect(v).toMatchObject({ tone: "success", caveat: false });
    expect(v?.text).toBe("Verified · document-suite · judged by google");
  });

  it("an answer nothing could examine asks for care without calling it a failure", () => {
    const v = epilogueVerdict(item({ verdict: "unverified" }));
    expect(v).toMatchObject({ tone: "warning", caveat: false });
    expect(v?.text).toContain("Unverified");
  });

  it("an answer with nothing to verify, or a stopped turn, says nothing about a check", () => {
    expect(epilogueVerdict(item())).toBeNull();
    expect(epilogueVerdict(item({ delivered: false }))).toBeNull();
  });
});
