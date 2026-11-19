/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { describe, expect, it } from "vitest";
import { mutateLoaded, spliceHead } from "./paging";

type Row = { id: string; at: string };
const at = (r: Row) => r.at;
const row = (id: string, at: string): Row => ({ id, at });

describe("spliceHead", () => {
  it("replaces the head and keeps the loaded tail", () => {
    const current = [row("b", "3"), row("a", "2"), row("old", "1")];
    const pageOne = [row("new", "4"), row("b", "3"), row("a", "2")];
    expect(spliceHead(current, pageOne, at)).toEqual([
      row("new", "4"),
      row("b", "3"),
      row("a", "2"),
      row("old", "1"),
    ]);
  });

  it("drops head rows that left the group", () => {
    // "gone" was in the loaded head but page one no longer carries it (its status moved it
    // to another group) - everything at or newer than the boundary is page one's to state.
    const current = [row("gone", "3"), row("a", "2"), row("old", "1")];
    const pageOne = [row("a", "2")];
    expect(spliceHead(current, pageOne, at)).toEqual([row("a", "2"), row("old", "1")]);
  });

  it("an empty page one empties the list - newest-first means there is nothing at all", () => {
    expect(spliceHead([row("a", "2")], [], at)).toEqual([]);
  });

  it("never duplicates a row that moved deeper than the boundary", () => {
    const current = [row("a", "2"), row("dup", "1")];
    const pageOne = [row("b", "3"), row("dup", "1")];
    const result = spliceHead(current, pageOne, at);
    expect(result.filter((r) => r.id === "dup")).toHaveLength(1);
  });
});

describe("mutateLoaded", () => {
  it("applies the mutation to a loaded list", () => {
    expect(
      mutateLoaded([row("a", "1"), row("b", "2")], (xs) => xs.filter((r) => r.id !== "a")),
    ).toEqual([row("b", "2")]);
  });

  it("leaves an unloaded list unloaded", () => {
    expect(mutateLoaded(null, (xs) => [row("a", "1"), ...xs])).toBeNull();
  });
});
