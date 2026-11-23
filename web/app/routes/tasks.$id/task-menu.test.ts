/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The ⋯ menu offers only the verbs the server would accept for THIS person (ADR-062): the
// detail read says what the caller may do (`can_edit`, `can_curate`, `is_creator`) and the
// menu believes it - a viewer never meets a Pause that answers 403.
import { describe, expect, it } from "vitest";
import type { TaskStatus } from "~/lib/api/events";
import { menuVerbs } from "./task-menu";

describe("menuVerbs", () => {
  it("the creator of a task in their own Project: everything", () => {
    expect(
      menuVerbs({ status: "running", canEdit: true, canCurate: true, isCreator: true }),
    ).toEqual({
      control: true,
      toggle: "pause",
      fork: true,
      rename: true,
      archive: true,
      delete: true,
    });
  });

  it("the Project owner tidies (archive, delete) but did not name the work (no rename)", () => {
    const v = menuVerbs({ status: "delivered", canEdit: true, canCurate: true, isCreator: false });
    expect(v).toMatchObject({ rename: false, archive: true, delete: true, fork: true });
  });

  it("an editor writes into the Project, and curates nothing of anyone else's", () => {
    const v = menuVerbs({ status: "running", canEdit: true, canCurate: false, isCreator: false });
    expect(v).toEqual({
      control: true,
      toggle: "pause",
      fork: true,
      rename: false,
      archive: false,
      delete: false,
    });
  });

  it("a viewer is offered nothing that writes", () => {
    const v = menuVerbs({ status: "running", canEdit: false, canCurate: false, isCreator: false });
    expect(v).toEqual({
      control: false,
      toggle: null,
      fork: false,
      rename: false,
      archive: false,
      delete: false,
    });
  });

  it("pause, resume and cancel are for a live task only", () => {
    expect(
      menuVerbs({ status: "delivered", canEdit: true, canCurate: true, isCreator: true }),
    ).toMatchObject({ control: false, toggle: null });
  });

  it("pause stops only a running or waiting task, and resume only a paused one", () => {
    const at = (status: TaskStatus) =>
      menuVerbs({ status, canEdit: true, canCurate: true, isCreator: true });
    expect(at("waiting")).toMatchObject({ control: true, toggle: "pause" });
    expect(at("paused")).toMatchObject({ control: true, toggle: "resume" });
    // A held task (created, not started) and one still planning can be cancelled, never paused.
    expect(at("created")).toMatchObject({ control: true, toggle: null });
    expect(at("planning")).toMatchObject({ control: true, toggle: null });
  });
});
