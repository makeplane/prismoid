/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The figure fleet's quiet fold (ADR-030): a working-role IMAGE lands
// on the record and the shelf but never mints a transcript card and never retires the
// streaming draft - a concurrent batch of PNGs must not bury the section being read. Every
// other artifact keeps the card + draft-retire behavior byte for byte, including working
// NON-images (sections), whose publish-opens-the-reader continuity is a designed feature.

import { describe, expect, it } from "vitest";
import type { TaskEvent } from "~/lib/api/events";
import { initialView, reduce, type TaskView } from "./task-view";

type Detail = Parameters<typeof initialView>[0];

const detail = (): Detail =>
  ({
    status: "running",
    goal: "a deck",
    cost: { total_usd: "0" },
    plan: { steps: [] },
  }) as unknown as Detail;

const ev = (frame: Record<string, unknown>): TaskEvent => frame as unknown as TaskEvent;

const fold = (view: TaskView, ...frames: TaskEvent[]): TaskView => frames.reduce(reduce, view);

const produced = (seq: number, over: Record<string, unknown> = {}): TaskEvent =>
  ev({
    type: "artifact.produced",
    seq,
    artifact_id: `art-${seq}`,
    title: `fig-s01-${seq}`,
    artifact_type: "image",
    role: "working",
    ...over,
  });

describe("artifact.produced role fold (review M10)", () => {
  it("a working image reaches the shelf but never the feed, and the draft survives", () => {
    let v = initialView(detail());
    v = fold(
      v,
      ev({ type: "artifact.snapshot", seq: 1, path: "/work/01.md", content: "draft text" }),
    );
    expect(v.draft?.text).toBe("draft text");
    v = fold(v, produced(2), produced(3), produced(4));
    expect(v.artifacts).toHaveLength(3);
    expect(v.artifacts.every((a) => a.role === "working")).toBe(true);
    expect(v.feed.filter((i) => i.kind === "artifact")).toHaveLength(0); // no cards minted
    expect(v.draft?.text).toBe("draft text"); // the reading surface survives the batch
  });

  it("a deliverable still mints its card and retires the draft", () => {
    let v = initialView(detail());
    v = fold(v, ev({ type: "artifact.snapshot", seq: 1, path: "/work/01.md", content: "draft" }));
    v = fold(v, produced(2, { artifact_type: "document", role: "deliverable", title: "The Deck" }));
    expect(v.feed.filter((i) => i.kind === "artifact")).toHaveLength(1);
    expect(v.draft).toBeNull();
  });

  it("a working NON-image becomes a CHIP row, consecutive sections fold into one (round six)", () => {
    let v = initialView(detail());
    v = fold(v, produced(2, { artifact_type: "document", role: "working", title: "01 Ports" }));
    v = fold(v, produced(3, { artifact_type: "document", role: "working", title: "02 Rails" }));
    v = fold(v, produced(3, { artifact_type: "document", role: "working", title: "02 Rails" }));
    expect(v.feed.filter((i) => i.kind === "artifact")).toHaveLength(0);
    const rows = v.feed.filter((i) => i.kind === "working-artifacts");
    expect(rows).toHaveLength(1);
    expect(rows[0].kind === "working-artifacts" && rows[0].items.map((c) => c.title)).toEqual([
      "01 Ports",
      "02 Rails",
    ]);
  });
});
