/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// Where "Open" lands for an inbox item. A PROJECT_SHARED knock ("Ada added you to Launch")
// has no task: it names its Project in its payload, and Open goes there.
import { describe, expect, it } from "vitest";
import { noQuestionsLine, projectTarget } from "./target";

describe("projectTarget", () => {
  it("a project_shared knock points at the Project its payload names", () => {
    expect(
      projectTarget({
        kind: "project_shared",
        project_id: "row-project",
        payload: { project_id: "sp-payload" },
      }),
    ).toBe("sp-payload");
  });

  it("a project_shared knock with an empty payload falls back to the row's own project", () => {
    expect(projectTarget({ kind: "project_shared", project_id: "sp-row", payload: {} })).toBe(
      "sp-row",
    );
  });

  it("a payload that is not an id is not trusted", () => {
    expect(
      projectTarget({
        kind: "project_shared",
        project_id: "sp-row",
        payload: { project_id: { evil: true } },
      }),
    ).toBe("sp-row");
  });

  it("any other item points at the project its row carries, or nowhere", () => {
    expect(projectTarget({ kind: "budget", project_id: "sp-row", payload: {} })).toBe("sp-row");
    expect(
      projectTarget({
        kind: "routine_paused",
        project_id: null,
        payload: { project_id: "ignored" },
      }),
    ).toBeNull();
  });
});

describe("noQuestionsLine", () => {
  it("a task waiting for its device says so, and never that it moved on", () => {
    const line = noQuestionsLine({ deviceAway: { title: "Waiting for Ada's Mac" } });
    expect(line).toBe("Waiting for Ada's Mac. Resume the task once it is back.");
    expect(line).not.toContain("moved on");
  });

  it("any other knock with no questions left is settled", () => {
    const settled = "These questions are settled; the task has moved on.";
    expect(noQuestionsLine(null)).toBe(settled);
    expect(noQuestionsLine({ deviceAway: null })).toBe(settled);
  });
});
