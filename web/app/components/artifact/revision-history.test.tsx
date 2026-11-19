/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The history's derived story (ADR-033), pinned pure: who made a revision, and that a
// restore is DERIVED (bytes equal an earlier revision's) - never a schema field.
import { describe, expect, it } from "vitest";
import { editsAllowed } from "./artifact-view";
import { revisionActions, revisionMeta } from "./revision-history";
import type { components } from "~/lib/api/types.gen";

type ArtifactOut = components["schemas"]["ArtifactOut"];

function rev(over: Partial<ArtifactOut> & { revision: number; sha256: string }): ArtifactOut {
  return {
    id: `id-${over.revision}`,
    task_id: "t",
    type: "document",
    title: "Notes",
    filename: "notes.md",
    render: "markdown",
    lineage_id: "lineage",
    label: null,
    has_preview: false,
    has_rendition: false,
    rendition_media: null,
    bytes: 10,
    provenance: { agent: "writer" },
    verdict: null,
    created_at: "2026-08-17T00:00:00Z",
    ...over,
  } as ArtifactOut;
}

// Newest first, ids from rev()'s own revision-derived rule.
const LINEAGE = [
  rev({ revision: 4, sha256: "aaa", provenance: { agent: "user" } }), // restored v1
  rev({ revision: 3, sha256: "ccc", provenance: { agent: "user" } }), // human edit
  rev({ revision: 2, sha256: "bbb" }), // AI follow-up
  rev({ revision: 1, sha256: "aaa" }), // as published
];

describe("revisionMeta - the derived story", () => {
  it("user provenance reads as you; agent names read as themselves; empty as the system", () => {
    expect(revisionMeta(LINEAGE, LINEAGE[1]).who).toBe("you");
    expect(revisionMeta(LINEAGE, LINEAGE[2]).who).toBe("writer");
    expect(revisionMeta(LINEAGE, rev({ revision: 5, sha256: "x", provenance: {} })).who).toBe(
      "the system",
    );
  });

  it("a restore is derived: same bytes as an EARLIER revision", () => {
    expect(revisionMeta(LINEAGE, LINEAGE[0]).restoredFrom).toBe(1);
  });

  it("an original is never a restore - later matches do not count backwards", () => {
    expect(revisionMeta(LINEAGE, LINEAGE[3]).restoredFrom).toBeNull();
    expect(revisionMeta(LINEAGE, LINEAGE[1]).restoredFrom).toBeNull();
  });
});

describe("revisionActions - a read-only reader offers no write (ADR-062 viewer mode)", () => {
  const head = LINEAGE[0];

  it("an editor labels any revision and restores any but the head", () => {
    expect(revisionActions(LINEAGE[2], head, false)).toEqual({ label: true, restore: true });
    expect(revisionActions(head, head, false)).toEqual({ label: true, restore: false });
  });

  it("read-only: no label, no restore - the server refuses both to a viewer", () => {
    expect(revisionActions(LINEAGE[2], head, true)).toEqual({ label: false, restore: false });
  });

  it("the content editor follows the same rule: the head, and only when not read-only", () => {
    expect(editsAllowed(true, false)).toBe(true);
    expect(editsAllowed(true, true)).toBe(false);
    expect(editsAllowed(false, false)).toBe(false);
  });
});
