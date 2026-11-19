/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/** The gate card's approve face learns scopes (ADR-046's web door): a browser
 * allowlist ask renders a three-way radio (once/task/always, "task" default) that decides
 * the accept body; a browser danger ask (and every connector gate) keeps the plain
 * accept/deny wire, no radios. Static-markup house idiom (question-dock.test.tsx); the pure
 * scope helpers (`scopeOptions`, `defaultScope`, `approveScopeContent`) live in
 * approval-dock.test.tsx - this file only covers the CARD's own markup, since the two
 * surfaces share one implementation of the helpers. */

import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";
import type { GateView } from "~/lib/task-view";
import { GateCard } from "./gate-card";

const gate = (over: Partial<GateView> = {}): GateView => ({
  gate_id: "g1",
  title: "Allow the browser to open sso.vendor.com?",
  kind: "approve",
  reason: "sso.vendor.com: browser.open\nopen sso.vendor.com/login",
  questions: [],
  ...over,
});

describe("the approve card's face", () => {
  it("a browser allowlist ask renders three radios, 'For this task' checked by default", () => {
    const html = renderToStaticMarkup(
      <GateCard
        gate={gate({
          scopes: ["once", "task", "always"],
          host: "sso.vendor.com",
        })}
      />,
    );
    expect(html).toContain("Just this once");
    expect(html).toContain("For this task");
    expect(html).toContain("Always allow sso.vendor.com");
    // the base-ui radio's native mirror input carries `checked` on the selected value only
    expect(html).toContain('checked="" value="task"');
    expect(html).not.toContain('checked="" value="once"');
    expect(html).not.toContain('checked="" value="always"');
  });

  it("a browser danger ask (scopes: []) renders no radios - plain Allow/Deny", () => {
    const html = renderToStaticMarkup(<GateCard gate={gate({ scopes: [] })} />);
    expect(html).not.toContain("Just this once");
    expect(html).not.toContain("For this task");
    expect(html).not.toContain("Always allow");
    expect(html).toContain(">Allow<");
    expect(html).toContain("Deny · continue without");
  });

  it("a connector gate (no scopes field at all) renders no radios either", () => {
    const html = renderToStaticMarkup(
      <GateCard
        gate={{
          gate_id: "g2",
          kind: "approve",
          title: "Allow Gmail: gmail_send?",
          reason: "",
          questions: [],
        }}
      />,
    );
    expect(html).not.toContain("Just this once");
    expect(html).toContain(">Allow<");
  });
});

describe("the budget gate's card face", () => {
  const budget = (over: Partial<GateView> = {}): GateView => ({
    gate_id: "g9",
    title: "Budget reached",
    kind: "budget",
    reason: "the task budget of $1 is spent ($1.20 so far); raise it and resume, or cancel",
    budget_scope: "task",
    questions: [],
    ...over,
  });

  it("never offers 'Decline · proceed anyway' (declining cancels the task)", () => {
    const html = renderToStaticMarkup(<GateCard gate={budget()} />);
    expect(html).not.toContain("Decline");
    expect(html).not.toContain("proceed anyway");
    expect(html).not.toContain("Submit answers");
    expect(html).toContain("the task budget of $1 is spent");
    expect(html).toContain("Cancel task");
  });

  it("with the task in hand it is the budget dock - raise and resume", () => {
    const html = renderToStaticMarkup(
      <MemoryRouter>
        <GateCard gate={budget()} budgetTask={{ id: "t1", projectId: "s1", spentUsd: 1.2 }} />
      </MemoryRouter>,
    );
    expect(html).toContain("Raise and resume");
    expect(html).toContain('value="2.20"');
    expect(html).not.toContain("Decline");
  });

  it("without it, honest Resume and Cancel task", () => {
    const html = renderToStaticMarkup(<GateCard gate={budget()} />);
    expect(html).toContain(">Resume<");
  });
});

describe("a CONTROL gate never offers Submit answers", () => {
  it("a CONTROL gate never wears the question face's submit", () => {
    const html = renderToStaticMarkup(
      <GateCard
        gate={gate({
          kind: "control",
          source: "handoff",
          title: "The agent needs someone to sign in",
        })}
      />,
    );
    expect(html).not.toContain("Submit answers");
  });

  it("a device's CONTROL gate offers Done instead (ADR-078)", () => {
    const html = renderToStaticMarkup(
      <GateCard
        isCreator
        gate={gate({
          kind: "control",
          source: "handoff",
          title: "The agent needs someone to sign in",
          machine_driver: "device",
        })}
      />,
    );
    expect(html).toContain(">Done<");
    const teammate = renderToStaticMarkup(
      <GateCard
        gate={gate({
          kind: "control",
          source: "handoff",
          title: "The agent needs someone to sign in",
          machine_driver: "device",
        })}
      />,
    );
    expect(teammate).not.toContain(">Done<"); // the creator's alone
    expect(html).not.toContain("Submit answers");
    const cloud = renderToStaticMarkup(
      <GateCard
        gate={gate({
          kind: "control",
          source: "handoff",
          title: "The agent needs someone to sign in",
        })}
      />,
    );
    expect(cloud).not.toContain(">Done<");
  });

  it("a budget gate (no questions) keeps its accept in the inbox", () => {
    const html = renderToStaticMarkup(
      <GateCard gate={gate({ kind: "budget", title: "Budget reached", questions: [] })} />,
    );
    expect(html).toContain("Submit answers");
  });

  it("a gate with questions still does", () => {
    const html = renderToStaticMarkup(
      <GateCard
        gate={gate({
          kind: "ask",
          title: "A question",
          questions: [
            { id: "q1", type: "text", prompt: "Which?", required: true },
          ] as GateView["questions"],
        })}
      />,
    );
    expect(html).toContain("Submit answers");
  });
});
