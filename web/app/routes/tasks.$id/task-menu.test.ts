/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The ⋯ menu offers only the verbs the server would accept for THIS person (ADR-062): the
// detail read says what the caller may do (`can_edit`, `can_curate`, `is_creator`) and the
// menu believes it - a viewer never meets a Pause that answers 403.
import { describe, expect, it } from "vitest";
import { menuVerbs } from "./task-menu";

describe("menuVerbs", () => {
  it("the creator of a task in their own Project: everything", () => {
    expect(menuVerbs({ live: true, canEdit: true, canCurate: true, isCreator: true })).toEqual({
      control: true,
      fork: true,
      rename: true,
      archive: true,
      delete: true,
    });
  });

  it("the Project owner tidies (archive, delete) but did not name the work (no rename)", () => {
    const v = menuVerbs({ live: false, canEdit: true, canCurate: true, isCreator: false });
    expect(v).toMatchObject({ rename: false, archive: true, delete: true, fork: true });
  });

  it("an editor writes into the Project, and curates nothing of anyone else's", () => {
    const v = menuVerbs({ live: true, canEdit: true, canCurate: false, isCreator: false });
    expect(v).toEqual({
      control: true,
      fork: true,
      rename: false,
      archive: false,
      delete: false,
    });
  });

  it("a viewer is offered nothing that writes", () => {
    const v = menuVerbs({ live: true, canEdit: false, canCurate: false, isCreator: false });
    expect(v).toEqual({
      control: false,
      fork: false,
      rename: false,
      archive: false,
      delete: false,
    });
  });

  it("pause, resume and cancel are for a live task only", () => {
    expect(
      menuVerbs({ live: false, canEdit: true, canCurate: true, isCreator: true }).control,
    ).toBe(false);
  });
});
