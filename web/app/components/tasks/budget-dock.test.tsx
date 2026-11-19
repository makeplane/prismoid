/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/** The budget dock (B3) - the "Budget reached" face and its pure helpers. */

import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";
import type { GateView } from "~/lib/task-view";
import { BudgetDock, budgetFace, parseBudget, suggestedBudget } from "./budget-dock";

const gate = (over: Partial<GateView> = {}): GateView => ({
  gate_id: "g1",
  title: "Budget reached",
  kind: "budget",
  reason: "the task budget of $0.01 is spent ($0.02 so far); raise it and resume, or cancel",
  budget_scope: "task",
  questions: [],
  ...over,
});

const render = (g: GateView) =>
  renderToStaticMarkup(
    <MemoryRouter>
      <BudgetDock gate={g} taskId="t1" projectId="s1" spentUsd={0.0234} />
    </MemoryRouter>,
  );

// The wave projection's gate: a $10 ceiling, $4 spent, a wave of about $8.
const wave = {
  ceiling_usd: "10.00",
  spent_usd: "4.00",
  projected_usd: "8.00",
};

describe("suggestedBudget", () => {
  it("is one dollar above what resuming needs: the live spend plus the wave, or the ceiling", () => {
    expect(suggestedBudget(4, wave)).toBe("13.00");
    expect(suggestedBudget(4.5, wave)).toBe("13.50"); // live spend moved on since the gate opened
    expect(
      suggestedBudget(0.0234, {
        ceiling_usd: "0.01",
        spent_usd: "0.02",
        projected_usd: "0",
      }),
    ).toBe("1.03");
  });
  it("never sits below the ceiling, so Raise never lowers it", () => {
    expect(
      suggestedBudget(2, {
        ceiling_usd: "10.00",
        spent_usd: "2.00",
        projected_usd: "0",
      }),
    ).toBe("11.00");
  });
  it("a gate with no figures (parked before them) is one dollar above the spend", () => {
    expect(suggestedBudget(0.0234, null)).toBe("1.03");
    expect(suggestedBudget(0)).toBe("1.00");
    expect(suggestedBudget(Number.NaN)).toBe("1.00");
  });
});

describe("budgetFace", () => {
  it("names the ceiling a gate hit, and reads anything else as the task's", () => {
    expect(budgetFace("project")).toBe("project");
    expect(budgetFace("workspace")).toBe("workspace");
    expect(budgetFace("task")).toBe("task");
    expect(budgetFace("")).toBe("task");
    expect(budgetFace(undefined)).toBe("task");
  });
});

describe("parseBudget", () => {
  it.each([
    ["5", "5.00"],
    ["$5", "5.00"],
    [" 2.5 ", "2.50"],
    ["0.01", "0.01"],
  ])("accepts %j as %j", (raw, want) => {
    expect(parseBudget(raw)).toBe(want);
  });
  it.each(["0", "-1", "abc", "1e3", "2.345", ""])("refuses %j", (raw) => {
    expect(parseBudget(raw)).toBeNull();
  });
});

describe("BudgetDock", () => {
  it("a task ceiling: the reason, a dollar field and Raise and resume", () => {
    const html = render(gate());
    expect(html).toContain("budget of $0.01 is spent");
    expect(html).toContain("Raise and resume");
    expect(html).toContain('value="1.03"');
    expect(html).toContain("Cancel task");
  });
  it("a gate the wave projection opened prefills a raise that clears the wave", () => {
    const html = renderToStaticMarkup(
      <MemoryRouter>
        <BudgetDock gate={gate({ budget_figures: wave })} taskId="t1" projectId="s1" spentUsd={4} />
      </MemoryRouter>,
    );
    expect(html).toContain('value="13.00"');
  });
  it("a Project ceiling: the owner raises it - a link, Resume, no field", () => {
    const html = render(gate({ budget_scope: "project" }));
    expect(html).toContain("only its owner can raise it");
    expect(html).toContain('href="/projects/s1?tab=settings"');
    expect(html).toContain("Resume");
    expect(html).not.toContain("Raise and resume");
    expect(html).not.toContain("<input");
  });
  it("a Project ceiling never shows the server's 'raise it and resume' line (C-I2)", () => {
    const html = render(
      gate({
        budget_scope: "project",
        reason: "the project budget of $5 is spent ($9 so far); raise it and resume, or cancel",
      }),
    );
    expect(html).toContain("only its owner can raise it");
    expect(html).not.toContain("raise it and resume");
    expect(html).not.toContain("budget of $5 is spent");
  });
  it("the Project face says Resume works once the owner has raised the budget (W3)", () => {
    const html = render(gate({ budget_scope: "project" }));
    expect(html).toContain("once the owner has raised it");
  });
  it("a workspace at its spending allowance: the reason and Cancel task alone - no field, no resume", () => {
    const reason =
      "This workspace has reached its spending allowance. The task picks up where it stopped once the allowance is back, or you can cancel it.";
    const html = render(
      gate({
        title: "Spending allowance reached",
        reason,
        budget_scope: "workspace",
        budget_figures: { ceiling_usd: "0", spent_usd: "0", exhausted: true },
      }),
    );
    expect(html).toContain("Spending allowance reached");
    expect(html).toContain(reason);
    expect(html).toContain("Cancel task");
    expect(html).not.toContain("Raise and resume");
    expect(html).not.toContain("Resume");
    expect(html).not.toContain("<input");
  });
  it("a Project gate the wave projection opened says the wave, not that the budget is spent", () => {
    const html = render(
      gate({
        budget_scope: "project",
        budget_figures: {
          ceiling_usd: "100.00",
          spent_usd: "95.00",
          projected_usd: "10.00",
        },
      }),
    );
    expect(html).toContain("The next wave would take the Project past its monthly budget");
    expect(html).not.toContain("monthly budget is spent");
    expect(html).toContain("only its owner can raise it");
  });
});
