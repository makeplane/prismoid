/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/** The peek's gate dock goes through `dockFor` like the task page. */

import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";
import type { GateView } from "~/lib/task-view";
import { PeekGateDock } from "./task-peek";

const gate = (over: Partial<GateView> = {}): GateView => ({
  gate_id: "g1",
  title: "Budget reached",
  kind: "budget",
  reason: "the task budget of $1 is spent ($1.20 so far); raise it and resume, or cancel",
  budget_scope: "task",
  questions: [],
  ...over,
});

const render = (g: GateView, canEdit = true) =>
  renderToStaticMarkup(
    <MemoryRouter>
      <PeekGateDock
        gate={g}
        taskId="t1"
        projectId="s1"
        spentUsd={1.2}
        canEdit={canEdit}
        isCreator
        creatorName={null}
      />
    </MemoryRouter>,
  );

describe("PeekGateDock", () => {
  it("a budget gate mounts the budget dock, never a question dock", () => {
    const html = render(gate());
    expect(html).toContain("Raise and resume");
    expect(html).toContain('value="2.20"');
  });
  it("a questionless non-budget gate mounts nothing (it used to crash the page)", () => {
    expect(render(gate({ kind: "input", budget_scope: undefined }))).toBe("");
  });
  it("an approve gate mounts the approval dock", () => {
    const html = render(
      gate({ kind: "approve", budget_scope: undefined, title: "Allow Gmail: gmail_send?" }),
    );
    expect(html).toContain("Allow");
    expect(html).not.toContain("Raise and resume");
  });
  it("a viewer sees no dock", () => {
    expect(render(gate(), false)).toBe("");
  });
});
