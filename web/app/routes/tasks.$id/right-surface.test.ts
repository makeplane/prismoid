/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { describe, expect, it } from "vitest";
import type { ArtifactView } from "~/lib/task-view";
import { latestPublish, opensOnPublish } from "./right-surface";

const art = (id: string, extra: Partial<ArtifactView> = {}) =>
  ({ id, type: "document", ...extra }) as ArtifactView;

describe("latestPublish", () => {
  it("keys the newest artifact by revision, skipping working images", () => {
    expect(latestPublish([])).toBeNull();
    expect(latestPublish([art("a"), art("b", { revision: 3 })])).toEqual({
      id: "b",
      key: "b:3",
    });
    expect(latestPublish([art("a"), art("fig", { role: "working", type: "image" })])).toEqual({
      id: "a",
      key: "a:1",
    });
  });
});

describe("opensOnPublish", () => {
  const open = { live: true, dismissed: null, superseded: {}, screenOpen: false };
  it("opens a fresh publish on a live task", () => {
    expect(opensOnPublish("a", open)).toBe(true);
  });
  it("respects a settled task, a close, a supersede and an open screen", () => {
    expect(opensOnPublish("a", { ...open, live: false })).toBe(false);
    expect(opensOnPublish("a", { ...open, dismissed: "a" })).toBe(false);
    expect(opensOnPublish("a", { ...open, dismissed: "b" })).toBe(true);
    expect(opensOnPublish("a", { ...open, superseded: { a: "Ada" } })).toBe(false);
    expect(opensOnPublish("a", { ...open, screenOpen: true })).toBe(false);
  });
});
