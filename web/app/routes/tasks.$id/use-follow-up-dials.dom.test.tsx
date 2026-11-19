/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// The follow-up box's dials: catalogs load only when the box can mint a follow-up, a fleet
// thread picks its model seats, a draft that names a service suggests it, the wire fields carry
// only moved dials, and a send clears the turn.
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "~/lib/api/client";
import { emitConnectorsChanged } from "~/lib/settings-bus";
import { useFollowUpDials } from "./use-follow-up-dials";

const reply = (data: unknown) =>
  Promise.resolve({ data, error: undefined, response: new Response(null, { status: 200 }) });

const model = (ref: string, cls: string) => ({ ref, classes: [cls] });
const MODELS = [
  model("m/fast", "fast-executor"),
  model("m/code", "code"),
  model("m/embed", "embedding"),
];
const CONNECTORS = {
  items: [
    { id: "c1", slug: "linear", name: "Linear", status: "live", suggest_words: ["linear"] },
    { id: "c2", slug: "old", name: "Old", status: "needs_reauth", suggest_words: ["old"] },
  ],
};
const CATALOG = { items: [{ slug: "notion", name: "Notion", keywords: ["notion"] }] };

let get: ReturnType<typeof vi.spyOn>;
const calledPaths = () => get.mock.calls.map((c: unknown[]) => c[0]);

beforeEach(() => {
  get = vi.spyOn(api, "GET").mockImplementation(((path: string) => {
    if (path === "/v1/models") return reply(MODELS);
    if (path === "/v1/connectors/catalog") return reply(CATALOG);
    return reply(CONNECTORS);
  }) as never);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const mount = (live: boolean, fleetThread: boolean) =>
  renderHook(() => useFollowUpDials({ live, fleetThread }));

describe("useFollowUpDials catalogs", () => {
  it("a live task fetches nothing", async () => {
    const { result } = mount(true, true);
    await new Promise((r) => setTimeout(r, 20));
    expect(get).not.toHaveBeenCalled();
    expect(result.current.catalog).toBeNull();
  });

  it("a settled non-fleet thread loads connectors but not the model catalog", async () => {
    const { result } = mount(false, false);
    await waitFor(() => expect(result.current.connectorCatalog).toHaveLength(2));
    expect(calledPaths()).not.toContain("/v1/models");
    expect(result.current.catalog).toBeNull();
  });

  it("a settled fleet thread loads only the executor models and selects them all", async () => {
    const { result } = mount(false, true);
    await waitFor(() => expect(result.current.catalog).toHaveLength(2));
    expect(result.current.modelRefs).toEqual(["m/fast", "m/code"]);
  });

  it("a failed model fetch means no dial, not a blocked box", async () => {
    get.mockImplementation(((path: string) =>
      path === "/v1/models"
        ? Promise.reject(new Error("down"))
        : reply(path.endsWith("catalog") ? CATALOG : CONNECTORS)) as never);
    const { result } = mount(false, true);
    await waitFor(() => expect(result.current.catalog).toEqual([]));
    expect(result.current.wireFields()).toEqual({});
  });

  it("a roster change on the settings bus re-reads the connectors", async () => {
    const { result } = mount(false, false);
    await waitFor(() => expect(result.current.connectorCatalog).not.toBeNull());
    const before = calledPaths().filter((p: unknown) => p === "/v1/connectors").length;
    act(() => emitConnectorsChanged());
    await waitFor(() =>
      expect(calledPaths().filter((p: unknown) => p === "/v1/connectors").length).toBe(before + 1),
    );
  });
});

describe("useFollowUpDials suggestion", () => {
  it("a draft naming a live connector suggests it; attaching removes the chip", async () => {
    const { result } = mount(false, false);
    await waitFor(() => expect(result.current.connectorCatalog).not.toBeNull());
    act(() => result.current.setDraft("file this in linear please"));
    await waitFor(() => expect(result.current.suggestion?.connectorId).toBe("c1"));
    act(() => result.current.attachConnector("c1"));
    expect(result.current.suggestion).toBeNull();
    expect(result.current.wireFields()).toEqual({ connectors: ["c1"] });
  });

  it("dismissing hides that suggestion, and emptying the draft re-arms it", async () => {
    const { result } = mount(false, false);
    await waitFor(() => expect(result.current.connectorCatalog).not.toBeNull());
    act(() => result.current.setDraft("use linear"));
    await waitFor(() => expect(result.current.suggestion?.slug).toBe("linear"));
    act(() => result.current.dismissSuggestion("linear"));
    expect(result.current.suggestion).toBeNull();
    act(() => result.current.setDraft(""));
    // the empty draft must settle past the debounce before the dismissal is forgotten
    await act(() => new Promise((r) => setTimeout(r, 320)));
    act(() => result.current.setDraft("use linear"));
    await waitFor(() => expect(result.current.suggestion?.slug).toBe("linear"));
  });
});

describe("useFollowUpDials wire fields", () => {
  it("an untouched box sends nothing; moved dials ride and a send clears them", async () => {
    const { result } = mount(false, true);
    await waitFor(() => expect(result.current.catalog).toHaveLength(2));
    expect(result.current.wireFields()).toEqual({});
    act(() => {
      result.current.setModelRefs(["m/fast"]);
      result.current.setTurnScale("auto");
      result.current.setTurnDeliverable("document:pdf");
      result.current.setConnectorIds(["c1"]);
    });
    expect(result.current.wireFields()).toEqual({
      models: ["m/fast"],
      connectors: ["c1"],
      scale: "auto",
      deliverable: "document",
      deliverable_format: "pdf",
    });
    act(() => result.current.clearTurn());
    expect(result.current.turnScale).toBeNull();
    expect(result.current.turnDeliverable).toBeNull();
    expect(result.current.connectorIds).toEqual([]);
    expect(result.current.wireFields()).toEqual({ models: ["m/fast"] });
  });

  it("a stale connector selection is dropped when the fresh roster lacks it", async () => {
    const { result } = mount(false, false);
    act(() => result.current.setConnectorIds(["c1", "c2", "gone"]));
    await waitFor(() => expect(result.current.connectorIds).toEqual(["c1"]));
  });
});
