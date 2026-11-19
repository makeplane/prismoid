/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { describe, expect, it } from "vitest";
import { followUpDialFields, liveConnectorIds, type TurnDials } from "./follow-up-dials";

const untouched: TurnDials = {
  fleetThread: false,
  catalog: null,
  modelRefs: [],
  connectorIds: [],
  turnScale: null,
  turnDeliverable: null,
};

describe("followUpDialFields", () => {
  it("sends nothing for an untouched box", () => {
    expect(followUpDialFields(untouched)).toEqual({});
  });
  it("sends models only when a fleet thread's picker narrowed them", () => {
    const catalog = [{ ref: "a" }, { ref: "b" }] as never;
    expect(
      followUpDialFields({ ...untouched, fleetThread: true, catalog, modelRefs: ["a", "b"] }),
    ).toEqual({});
    expect(
      followUpDialFields({ ...untouched, fleetThread: true, catalog, modelRefs: ["a"] }),
    ).toEqual({ models: ["a"] });
    expect(followUpDialFields({ ...untouched, catalog, modelRefs: ["a"] })).toEqual({});
  });
  it("sends the moved dials", () => {
    expect(
      followUpDialFields({
        ...untouched,
        connectorIds: ["c1"],
        turnScale: "auto",
        turnDeliverable: "document:pdf",
      }),
    ).toEqual({
      connectors: ["c1"],
      scale: "auto",
      deliverable: "document",
      deliverable_format: "pdf",
    });
  });
});

describe("liveConnectorIds", () => {
  it("keeps only present, live selections", () => {
    const catalog = [
      { id: "a", status: "live" },
      { id: "b", status: "needs_reauth" },
    ] as never;
    expect(liveConnectorIds(["a", "b", "gone"], catalog)).toEqual(["a"]);
  });
});
