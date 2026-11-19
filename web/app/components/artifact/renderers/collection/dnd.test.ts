/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { describe, expect, it } from "vitest";
import { moveAnchorFor } from "./dnd";

describe("moveAnchorFor (sortable drop → neighbor anchor)", () => {
  const ids = ["a", "b", "c", "d"];

  it("dragging down anchors before over's successor, or after over when it is last", () => {
    expect(moveAnchorFor(ids, "a", "c")).toEqual({ beforeId: "d" });
    expect(moveAnchorFor(ids, "b", "d")).toEqual({ afterId: "d" });
  });

  it("dragging up anchors before over", () => {
    expect(moveAnchorFor(ids, "d", "b")).toEqual({ beforeId: "b" });
  });

  it("same id or unknown ids stage nothing", () => {
    expect(moveAnchorFor(ids, "b", "b")).toBeNull();
    expect(moveAnchorFor(ids, "ghost", "b")).toBeNull();
    expect(moveAnchorFor(ids, "b", "ghost")).toBeNull();
  });
});
