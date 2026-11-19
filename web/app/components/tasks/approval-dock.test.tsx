/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/** The approval dock (ADR-054) - the approve gate's face and its pure parse. */

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { GateView } from "~/lib/task-view";
import {
  ApprovalDock,
  approvalAccess,
  approvalHeading,
  approvalReason,
  approveScopeContent,
  defaultScope,
  parseApprovalItems,
  scopeOptions,
  supersededNote,
} from "./approval-dock";
import { GateCard } from "./gate-card";

const gate = (over: Partial<GateView> = {}): GateView => ({
  gate_id: "g1",
  title: "Allow Gmail: gmail_send_email?",
  kind: "approve",
  reason: "Gmail: gmail_send_email\nbody: hello there\nto: a@b.c",
  questions: [],
  ...over,
});

describe("the reason parse", () => {
  it("one block per call: label line + preview lines, blank-line separated", () => {
    const items = parseApprovalItems(
      gate({
        reason: "Gmail: send\nto: a@b.c\n\nNotion: create_page\ntitle: Notes",
      }),
    );
    expect(items).toEqual([
      { label: "Gmail: send", preview: "to: a@b.c" },
      { label: "Notion: create_page", preview: "title: Notes" },
    ]);
  });

  it("an empty reason renders one block wearing the title - the dock never hides a gate", () => {
    expect(parseApprovalItems(gate({ reason: "" }))).toEqual([
      { label: "Allow Gmail: gmail_send_email?", preview: "" },
    ]);
    expect(approvalHeading(gate({ title: undefined }))).toBe("Waiting on your approval");
  });
});

describe("scopeOptions", () => {
  it("keeps the fixed once/task/always order regardless of the gate's own ordering", () => {
    const g = gate({
      scopes: ["always", "once", "task"],
      host: "sso.vendor.com",
    });
    expect(scopeOptions(g).map((o) => o.value)).toEqual(["once", "task", "always"]);
  });

  it("labels 'always' with the host; the others are host-independent", () => {
    const g = gate({
      scopes: ["once", "task", "always"],
      host: "sso.vendor.com",
    });
    expect(scopeOptions(g)).toEqual([
      { value: "once", label: "Just this once" },
      { value: "task", label: "For this task" },
      { value: "always", label: "Always allow sso.vendor.com" },
    ]);
  });

  it("a danger ask (scopes: []) offers nothing; nor does a connector gate", () => {
    expect(scopeOptions(gate({ scopes: [] }))).toEqual([]);
    expect(scopeOptions(gate({}))).toEqual([]); // scopes absent entirely (a connector gate)
  });
});

describe("defaultScope", () => {
  it("picks 'task' when the gate offers it", () => {
    expect(defaultScope(scopeOptions(gate({ scopes: ["once", "task", "always"] })))).toBe("task");
  });

  it("falls back to the first offered scope when 'task' is not one of them", () => {
    expect(defaultScope(scopeOptions(gate({ scopes: ["once", "always"] })))).toBe("once");
  });

  it("falls back to 'task' itself when nothing is offered - a gate never renders unselected", () => {
    expect(defaultScope(scopeOptions(gate({ scopes: [] })))).toBe("task");
  });
});

describe("approveScopeContent", () => {
  it("carries the chosen scope when the gate offered any", () => {
    expect(approveScopeContent(gate({ scopes: ["once", "task", "always"] }), "once")).toEqual({
      scope: "once",
    });
  });

  it('is undefined for a danger ask - the plain {action: "accept"} wire, unchanged', () => {
    expect(approveScopeContent(gate({ scopes: [] }), "task")).toBeUndefined();
    expect(approveScopeContent(gate({}), "task")).toBeUndefined();
  });
});

describe("the dock's face", () => {
  it("an approve gate offers Allow and Deny - never Submit answers, never a stepper", () => {
    const html = renderToStaticMarkup(<ApprovalDock gate={gate()} />);
    expect(html).toContain(">Allow<");
    expect(html).toContain(">Deny<");
    expect(html).not.toContain("Submit answers");
    expect(html).not.toContain("of 2"); // no stepper: one gate, one decision
  });

  it("says first why the rules asked, above the evidence; a gate without a rule says nothing", () => {
    const asked = gate({ asks_because: ["it moves money", "this task read outside text"] });
    expect(approvalReason(asked)).toBe("Asks because it moves money; this task read outside text.");
    expect(approvalReason(gate())).toBeNull();
    const html = renderToStaticMarkup(<ApprovalDock gate={asked} />);
    expect(html).toContain("Asks because it moves money; this task read outside text.");
    expect(html.indexOf("Asks because")).toBeLessThan(html.indexOf("to: a@b.c"));
    expect(renderToStaticMarkup(<GateCard gate={asked} />)).toContain(
      "Asks because it moves money",
    );
  });

  it("the args preview renders mono, capped, scrollable - the evidence is readable", () => {
    const html = renderToStaticMarkup(<ApprovalDock gate={gate()} />);
    expect(html).toContain("font-mono");
    expect(html).toContain("max-h-40");
    expect(html).toContain("to: a@b.c");
  });

  it("denying is explained as a skip, not a stop - the task continues", () => {
    const html = renderToStaticMarkup(<ApprovalDock gate={gate()} />);
    expect(html).toContain("the task continues");
  });

  it("a browser allowlist ask renders the three-way radio, 'For this task' checked", () => {
    const html = renderToStaticMarkup(
      <ApprovalDock
        gate={gate({
          scopes: ["once", "task", "always"],
          host: "sso.vendor.com",
        })}
      />,
    );
    expect(html).toContain("Just this once");
    expect(html).toContain("For this task");
    expect(html).toContain("Always allow sso.vendor.com");
    expect(html).toContain('checked="" value="task"');
  });

  it("a connector gate (no scopes) renders no radio - plain Allow/Deny, unchanged", () => {
    const html = renderToStaticMarkup(<ApprovalDock gate={gate()} />);
    expect(html).not.toContain("Just this once");
    expect(html).not.toContain("For this task");
  });
});

describe("who may allow it (ADR-062: the dock offers no verb the server refuses)", () => {
  it("a connector approval is the creator's alone: others see who they wait on, and Deny", () => {
    const html = renderToStaticMarkup(
      <ApprovalDock gate={gate({ source: "connector" })} isCreator={false} creatorName="Ada" />,
    );
    expect(html).not.toContain(">Allow<");
    expect(html).toContain(">Deny<");
    expect(html).toContain("Waiting on Ada");
  });

  it("an unnamed creator still reads as someone to wait on, never a blank", () => {
    const html = renderToStaticMarkup(
      <ApprovalDock gate={gate()} isCreator={false} creatorName={null} />,
    );
    expect(html).toContain("Waiting on the task&#x27;s creator");
  });

  it("the creator keeps Allow on their own connector approval", () => {
    const html = renderToStaticMarkup(
      <ApprovalDock gate={gate({ source: "connector" })} isCreator creatorName="Ada" />,
    );
    expect(html).toContain(">Allow<");
    expect(html).not.toContain("Waiting on");
  });

  it("a browser approval stays open to editors, once or for the task - never always", () => {
    const html = renderToStaticMarkup(
      <ApprovalDock
        gate={gate({
          source: "browser",
          scopes: ["once", "task", "always"],
          host: "sso.vendor.com",
        })}
        isCreator={false}
        creatorName="Ada"
      />,
    );
    expect(html).toContain(">Allow<");
    expect(html).toContain("Just this once");
    expect(html).toContain("For this task");
    expect(html).not.toContain("Always allow");
  });

  it("approvalAccess is the rule, pure", () => {
    const browser = gate({
      source: "browser",
      scopes: ["once", "always"],
      host: "h",
    });
    expect(approvalAccess(browser, true).options.map((o) => o.value)).toEqual(["once", "always"]);
    expect(approvalAccess(browser, false)).toEqual({
      allow: true,
      options: [{ value: "once", label: "Just this once" }],
    });
    expect(approvalAccess(gate(), false)).toEqual({
      allow: false,
      options: [],
    });
    expect(approvalAccess(gate(), true).allow).toBe(true);
  });
});

describe("the inbox card's approve face", () => {
  it("keeps all three verbs - Allow, Deny, and the Cancel task escape hatch", () => {
    const html = renderToStaticMarkup(<GateCard gate={gate()} />);
    expect(html).toContain(">Allow<");
    expect(html).toContain("Deny · continue without");
    expect(html).toContain("Cancel task");
    expect(html).not.toContain("Submit answers");
  });

  it("a question gate keeps its question face - the approve branch never leaks", () => {
    const html = renderToStaticMarkup(
      <GateCard
        gate={{
          gate_id: "g2",
          kind: "input",
          title: "A few questions",
          questions: [
            {
              id: "q1",
              prompt: "Which one?",
              type: "text",
              options: [],
              required: true,
              allow_other: false,
            },
          ],
          reason: "",
        }}
      />,
    );
    expect(html).toContain("Submit answers");
    expect(html).not.toContain(">Allow<");
  });
});

// A steer that lands while an approval waits closes it as a decline naming the steer.
// A late Allow is handed THAT resolution (HTTP 200) - the card must read it as superseded, by
// whose steer, and never as "approved" or "answered first".
describe("supersededNote - a late Allow's answer", () => {
  it("a decline naming a steer reads as superseded, by the steerer", () => {
    expect(
      supersededNote({
        action: "decline",
        content: { superseded_by: "c1" },
        resolved_by: "Bob",
      }),
    ).toBe("Superseded by Bob's steer");
    expect(
      supersededNote({
        action: "decline",
        content: { superseded_by: "c1" },
        resolved_by: "",
      }),
    ).toBe("Superseded by a steer");
  });

  it("every other answer is not a supersede", () => {
    expect(supersededNote({ action: "accept", content: null, resolved_by: "Bob" })).toBeNull();
    expect(supersededNote({ action: "decline", content: null, resolved_by: "Bob" })).toBeNull();
    expect(
      supersededNote({
        action: "decline",
        content: { scope: "task" },
        resolved_by: "Bob",
      }),
    ).toBeNull();
  });
});
