/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// One switch picks a gate's face for every host - the task page, the peek, a bot chat's card.
// A host that picked by hand mounted the question stepper for an approve, connect or budget
// gate, and the stepper indexed an empty question list and took the whole page down. Static
// markup, the house idiom (no DOM driver).

import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";
import type { Question } from "~/lib/api/events";
import type { GateView } from "~/lib/task-view";
import { GateDock } from "./gate-dock";
import { QuestionDock } from "./question-dock";

const ask: Question = {
  id: "runtime",
  prompt: "Where will this run?",
  type: "single_select",
  required: true,
  allow_other: false,
  options: [{ value: "kvm", label: "Bare-metal Linux w/ KVM", description: null }],
};

const gate = (over: Partial<GateView> = {}): GateView => ({
  gate_id: "g1",
  kind: "input",
  title: "A question",
  questions: [],
  ...over,
});

const render = (g: GateView) =>
  renderToStaticMarkup(
    <MemoryRouter>
      <GateDock gate={g} />
    </MemoryRouter>,
  );

describe("the gate's one face", () => {
  it("a budget park has no face, and never takes its host down", () => {
    expect(render(gate({ title: "Budget reached" }))).toBe("");
  });

  it("an approve gate wears the approval dock, never the stepper", () => {
    const html = render(gate({ kind: "approve", reason: "Gmail: send\nto: a@b.c" }));
    expect(html).toContain(">Allow<");
    expect(html).not.toContain("Submit answers");
  });

  it("a connect gate wears the connect block", () => {
    const html = render(
      gate({
        kind: "connect",
        connector_slug: "",
        title: "Connect Teleporter to continue?",
      }),
    );
    expect(html).toContain("Connection needed");
    expect(html).not.toContain(">Skip<");
  });

  it("a budget gate wears the budget dock where its host passes the task's figures", () => {
    const budget = gate({
      kind: "budget",
      title: "Budget reached",
      budget_scope: "task",
    });
    const html = renderToStaticMarkup(
      <MemoryRouter>
        <GateDock gate={budget} budget={{ taskId: "t1", projectId: null, spentUsd: 1.2 }} />
      </MemoryRouter>,
    );
    expect(html).toContain("Raise and resume");
    expect(render(budget)).toBe("");
  });

  it("a control gate has no face here: a hand-back answers it, on the task page's screen", () => {
    expect(
      render(
        gate({
          kind: "control",
          source: "handoff",
          title: "The agent needs someone",
        }),
      ),
    ).toBe("");
  });

  it("questions wear the stepper", () => {
    expect(render(gate({ questions: [ask] }))).toContain("Where will this run?");
  });
});

describe("the question dock is total", () => {
  it("renders nothing for a gate with no questions", () => {
    expect(renderToStaticMarkup(<QuestionDock gate={gate()} />)).toBe("");
  });
});
