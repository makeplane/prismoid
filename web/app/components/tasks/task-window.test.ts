/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { describe, expect, it } from "vitest";
import { mergeTaskWindow, rowVerbs, toTaskRow, type TaskWindowRow } from "./task-window";

const row = (id: string, createdAt: string, pinned = false): TaskWindowRow => ({
  id,
  label: `Task ${id}`,
  status: "running",
  pinned,
  createdAt,
});

describe("mergeTaskWindow", () => {
  it("interleaves pinned and unpinned tasks by recency", () => {
    expect(
      mergeTaskWindow(
        [row("older-pin", "2026-07-29T10:00:00Z", true)],
        [row("newest", "2026-07-29T12:00:00Z"), row("oldest", "2026-07-29T08:00:00Z")],
      ).map((task) => task.id),
    ).toEqual(["newest", "older-pin", "oldest"]);
  });

  it("deduplicates a task while an optimistic move races a refresh", () => {
    const pinned = row("same", "2026-07-29T12:00:00Z", true);
    const staleRecent = row("same", "2026-07-29T12:00:00Z");
    expect(mergeTaskWindow([pinned], [staleRecent])).toEqual([pinned]);
  });
});

describe("rowVerbs - the sidebar offers only what the server allows this caller", () => {
  const mine = { ...row("a", "2026-07-29T12:00:00Z"), creatorId: "u-ada" };
  const bobs = { ...row("b", "2026-07-29T12:00:00Z"), creatorId: "u-bob" };

  it("your own task: rename, pin, delete", () => {
    expect(rowVerbs(mine, "u-ada")).toEqual({
      rename: true,
      pin: true,
      delete: true,
    });
  });

  it("a teammate's Project task: none of them (all creator-only on the server)", () => {
    expect(rowVerbs(bobs, "u-ada")).toEqual({
      rename: false,
      pin: false,
      delete: false,
    });
  });

  it("an unknown caller or creator offers nothing rather than a verb that 403s", () => {
    expect(rowVerbs(mine, undefined)).toEqual({
      rename: false,
      pin: false,
      delete: false,
    });
    expect(rowVerbs(row("c", "2026-07-29T12:00:00Z"), "u-ada")).toEqual({
      rename: false,
      pin: false,
      delete: false,
    });
  });

  it("toTaskRow carries the creator off the wire", () => {
    const summary = {
      id: "t",
      title: "T",
      goal: "g",
      status: "running",
      pinned_at: null,
      created_at: "2026-07-29T12:00:00Z",
      creator_id: "u-ada",
    } as unknown as Parameters<typeof toTaskRow>[0];
    expect(toTaskRow(summary).creatorId).toBe("u-ada");
  });
});
