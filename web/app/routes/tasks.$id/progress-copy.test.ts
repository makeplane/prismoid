/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The tree's voice is data - these pin the verb tables, the summary parsing, and the
// ledger grammar so a copy tweak is a deliberate act, never a drive-by. (The duration
// grammar's pins live with the law, in ~/lib/time.test.ts.)
import { AlertTriangle, Pause } from "lucide-react";
import { describe, expect, it } from "vitest";
import {
  actorLabel,
  connectorToolCopy,
  agentHueIndex,
  doingLine,
  waitingPhrase,
  groupLedger,
  headline,
  isHeld,
  outcomeLabel,
  pauseStatusLine,
  rootLabel,
  toolIcon,
  toolLine,
  turnDuration,
  turnTokens,
} from "./progress-copy";
import { nameHue } from "~/components/kit/tone";
import type { ExecGroup, ExecRow, ExecutionItem } from "~/lib/task-view";

const tool = (over: Partial<Extract<ExecRow, { t: "tool" }>>): Extract<ExecRow, { t: "tool" }> => ({
  t: "tool",
  id: "tc1",
  tool: "frame.read_file",
  ...over,
});

const group = (over: Partial<ExecGroup>): ExecGroup => ({
  id: "s1",
  kind: "step",
  title: "Write the memo",
  state: "done",
  attempt: 1,
  rows: [],
  ...over,
});

describe("the tool row's line", () => {
  it("names the web tools, rather than falling back to the raw journal name", () => {
    // Regression: the backend shipped the web tools with no rows here, so every
    // fetch and search rendered as "Called web.fetch" under a wrench. The fallback is for
    // tools nobody registered - a tool the engine really has gets a verb.
    expect(toolLine(tool({ tool: "web.fetch", summary: "example.com/a" }))).toMatchObject({
      verb: "Read",
      detail: "example.com/a",
    });
    expect(
      toolLine(tool({ tool: "web.fetch", pending: true, summary: "example.com/a" })).verb,
    ).toBe("Reading");
    // A settled search row's summary is the engine, never the query: it reads as the route.
    expect(toolLine(tool({ tool: "web.search", summary: "brave" }))).toEqual({
      verb: "Searched the web",
      detail: "via brave",
      mono: false,
    });
    expect(toolLine(tool({ tool: "web.search", ok: false, summary: "openai" })).detail).toBe(
      "web search via openai",
    );
    // The live label is a count of queries: it keeps its own words.
    expect(toolLine(tool({ tool: "web.search", pending: true, summary: "5 queries" }))).toEqual({
      verb: "Searching",
      detail: "5 queries",
      mono: true,
    });
    expect(toolLine(tool({ tool: "media.generate_image", summary: "/o/a.png" })).verb).toBe("Drew");
    // The fallback still holds the line for a name the engine does NOT have.
    expect(toolLine(tool({ tool: "teleport_user" })).verb).toBe("teleport_user");
  });

  it("speaks the browser family in plain verbs, either spelling of the name", () => {
    // Regression: a page visit read "Calling args: url" live and "browser_open args: url"
    // settled - the model's name and its argument keys, under a wrench.
    expect(toolLine(tool({ tool: "browser_open", pending: true, summary: "args: url" }))).toEqual({
      verb: "Opening",
      detail: "a page",
      mono: false,
    });
    expect(toolLine(tool({ tool: "browser.open", summary: "example.com/pricing" }))).toEqual({
      verb: "Opened",
      detail: "example.com/pricing",
      mono: true,
    });
    expect(toolLine(tool({ tool: "browser.back", summary: "back" }))).toMatchObject({
      verb: "Went back",
      detail: "",
    });
    expect(toolLine(tool({ tool: "browser.scroll", summary: "scroll down" })).detail).toBe("down");
    expect(toolLine(tool({ tool: "browser.screenshot", summary: "screenshot" })).verb).toBe(
      "Took a screenshot",
    );
  });

  it("never shows an argument list, on any row", () => {
    expect(
      toolLine(tool({ tool: "web.fetch", pending: true, summary: "args: url" })),
    ).toMatchObject({ verb: "Reading", detail: "" });
    expect(toolLine(tool({ tool: "teleport_user", pending: true, summary: "args: who" }))).toEqual({
      verb: "Calling",
      detail: "teleport_user",
      mono: true,
    });
    expect(toolLine(tool({ tool: "teleport_user", summary: "args: who" })).detail).toBe("");
  });

  it("speaks the Machine's phases as machinery sentences", () => {
    // "Waking the Machine" is the whole line, in the UI face - never "Called machine".
    expect(
      toolLine(tool({ tool: "machine", pending: true, summary: "Waking the Machine" })),
    ).toEqual({ verb: "", detail: "Waking the Machine", mono: false });
  });

  it("speaks the live verb while pending and the settled verb after", () => {
    expect(toolLine(tool({ pending: true, summary: "/task/inputs/brief.md" })).verb).toBe(
      "Reading",
    );
    expect(toolLine(tool({ summary: "/task/inputs/brief.md" })).verb).toBe("Read");
  });

  it("reads publish summaries back as file · kind", () => {
    const { verb, detail } = toolLine(
      tool({ tool: "frame.publish", summary: "document:/work/memo.md" }),
    );
    expect(verb).toBe("Published");
    expect(detail).toBe("memo.md · document");
  });

  it("strips the host= prefix from egress rows", () => {
    expect(toolLine(tool({ tool: "frame.egress", summary: "host=axios.com" })).detail).toBe(
      "axios.com",
    );
  });

  it("an egress refusal reads Blocked with the reason attached", () => {
    const { verb, detail } = toolLine(
      tool({
        tool: "frame.egress",
        summary: "host=pypi.org",
        ok: false,
        deniedReason: "not in the allowlist",
      }),
    );
    expect(verb).toBe("Blocked");
    expect(detail).toBe("pypi.org - not in the allowlist");
  });

  it("a failed call reads Failed, never Denied, and drops the exception's class name", () => {
    // Regression: a browser timeout read "Denied … BrowserFailed: browser timed out" - as if
    // someone had said no, in the engine's own vocabulary.
    expect(
      toolLine(
        tool({
          tool: "browser.open",
          summary: "example.com/pricing",
          ok: false,
          deniedReason: "BrowserFailed: browser timed out after 30.0s",
        }),
      ),
    ).toEqual({
      verb: "Failed",
      detail: "example.com/pricing - browser timed out after 30.0s",
      mono: false,
    });
    expect(toolLine(tool({ tool: "web.fetch", summary: "example.com/a", ok: false })).detail).toBe(
      "example.com/a",
    );
  });

  it("a tool the step was not granted reads Not allowed, without the grant list", () => {
    expect(
      toolLine(
        tool({
          tool: "web.fetch",
          summary: "example.com/a",
          ok: false,
          deniedReason: "not granted (has: ['frame.shell'])",
        }),
      ),
    ).toEqual({ verb: "Not allowed", detail: "example.com/a", mono: false });
  });

  it("an unknown tool falls back to its raw name - visible, never invented", () => {
    // An unknown VERB keeps the wrench truth even under a plausible slug (the
    // connector branch speaks only verbs it recognizes; flailing stays visible).
    expect(toolLine(tool({ tool: "hallucinated_teleport" })).verb).toBe("hallucinated_teleport");
  });
});

describe("connector tool voice (ADR-054)", () => {
  it("gmail_send_email reads as a sent mail, never Called gmail_send_email", () => {
    expect(connectorToolCopy("gmail_send_email")).toMatchObject({
      live: "Sending via Gmail",
      done: "Sent via Gmail",
    });
    expect(toolLine(tool({ tool: "gmail_send_email" })).verb).toBe("Sent via Gmail");
  });

  it("a known slug wears its brand name; an unknown slug capitalizes honestly", () => {
    expect(connectorToolCopy("notion_search")?.done).toBe("Searched Notion");
    expect(connectorToolCopy("mycrm_search")?.done).toBe("Searched Mycrm");
  });

  it("the table stays first authority - ask_user and dotted journal names never route here", () => {
    expect(connectorToolCopy("ask_user")).toBeNull();
    expect(connectorToolCopy("web.fetch")).toBeNull();
    expect(connectorToolCopy("frame.read_file")).toBeNull();
  });

  it("an unknown verb under a known slug keeps the wrench truth", () => {
    expect(connectorToolCopy("gmail_teleport")).toBeNull();
  });
});

describe("headlines and the root line", () => {
  it("the plan group counts its steps once done", () => {
    expect(
      headline(
        group({
          id: "plan",
          kind: "plan",
          title: "",
          checklist: [
            { id: "a", title: "one", state: "done" },
            { id: "b", title: "two", state: "done" },
          ],
        }),
      ),
    ).toBe("Planned 2 steps");
  });

  it("a step group speaks the planner's own title", () => {
    expect(headline(group({ title: "Research the brief" }))).toBe("Research the brief");
  });

  it("the settled root counts steps and names issues honestly", () => {
    expect(rootLabel({ settled: true, groups: [group({}), group({ id: "s2" })] })).toBe(
      "Completed 2 steps",
    );
    expect(
      rootLabel({
        settled: true,
        groups: [group({}), group({ id: "s2", state: "escalated" })],
      }),
    ).toBe("1 of 2 steps done · 1 issue");
  });

  it("a turn that ended on an issue never says Completed", () => {
    // Regression: a failed task's only step escalated and the root still read
    // "Completed 1 step · 1 issue".
    expect(rootLabel({ settled: true, groups: [group({ state: "escalated" })] })).toBe(
      "0 of 1 step done · 1 issue",
    );
  });

  it("counts the plan's steps when a later step never started", () => {
    // The second step never got a group: the rail read "0/2" while the root read "0 of 1".
    const plan = group({
      id: "plan",
      kind: "plan",
      title: "",
      checklist: [
        { id: "s1", title: "Check the facts", state: "escalated" },
        { id: "s2", title: "Write the memo", state: "pending" },
      ],
    });
    expect(rootLabel({ settled: true, groups: [plan, group({ state: "escalated" })] })).toBe(
      "0 of 2 steps done · 1 issue",
    );
  });

  it("skipped steps are NAMED, never absorbed into Completed", () => {
    expect(
      rootLabel({
        settled: true,
        groups: [group({}), group({ id: "s2", state: "skipped" })],
      }),
    ).toBe("Completed 1 step · 1 skipped");
  });

  it("the live root names the position AND who is working", () => {
    expect(
      rootLabel({
        settled: false,
        groups: [group({ state: "done" }), group({ id: "s2", state: "running", agent: "writer" })],
      }),
    ).toBe("Working - step 2 of 2 · writer");
  });

  it("a parallel fan-out is NAMED - N agents in parallel", () => {
    expect(
      rootLabel({
        settled: false,
        groups: [
          group({ id: "s1", state: "running", agent: "writer" }),
          group({ id: "s2", state: "running", agent: "writer" }),
          group({ id: "s3", state: "running", agent: "writer" }),
        ],
      }),
    ).toBe("Working - 3 agents in parallel · 0 of 3 done");
  });

  it("the settled root counts the distinct agents that worked", () => {
    expect(
      rootLabel({
        settled: true,
        groups: [
          group({
            id: "plan",
            kind: "plan",
            title: "",
            agent: "planner",
          }),
          group({ id: "s1", agent: "researcher" }),
          group({ id: "s2", agent: "writer" }),
        ],
      }),
    ).toBe("Completed 2 steps · 3 agents");
  });

  it("parked states tell the truth - waiting/paused, never 'Working'", () => {
    const groups = [
      group({ state: "done" }),
      group({ id: "s2", state: "running", agent: "writer" }),
    ];
    expect(rootLabel({ settled: false, groups }, "waiting")).toBe(
      "Waiting for your answer - 1 of 2 done",
    );
    expect(rootLabel({ settled: false, groups }, "paused")).toBe("Paused - 1 of 2 done");
  });

  it("the per-group ledger reads model calls · searches · tokens · cost", () => {
    expect(groupLedger(group({ calls: 3, tokens: 5230, costUsd: 0.018 }))).toBe(
      "3 model calls · 5.2k tok · $0.018",
    );
    expect(groupLedger(group({}))).toBeNull();
  });

  it("the ledger shows the cached tokens that carry the cost, and searches apart", () => {
    expect(groupLedger(group({ calls: 1, tokens: 22, cachedTokens: 12_800, costUsd: 0.04 }))).toBe(
      "1 model call · 22 tok + 12.8k cached · $0.040",
    );
    expect(groupLedger(group({ calls: 6, searches: 2, tokens: 3400 }))).toBe(
      "6 model calls · 2 searches · 3.4k tok",
    );
  });

  it("machinery actors have names the tree can speak", () => {
    expect(actorLabel("clarifier")).toBe("Clarifier");
    expect(actorLabel("planner")).toBe("Planner");
    expect(actorLabel("namer")).toBe("Namer");
    expect(actorLabel("frame.read_file")).toBeNull();
  });
});

// The tail line's pure voice: time · tokens · what it's doing.
describe("the tail line helpers", () => {
  const g = (over: Partial<ExecGroup> = {}): ExecGroup => ({
    id: "s1",
    kind: "step",
    title: "Write the memo",
    state: "running",
    agent: "writer",
    attempt: 1,
    rows: [],
    ...over,
  });

  it("turnTokens sums every group's folded ledger into one figure", () => {
    const item = {
      kind: "execution",
      id: "x",
      turn: 1,
      settled: false,
      groups: [g({ tokens: 6400 }), g({ id: "s2", tokens: 104_400 })],
    } as ExecutionItem;
    expect(turnTokens(item)).toBe("110.8k tok");
    expect(turnTokens({ ...item, groups: [g({ tokens: undefined })] })).toBeNull();
  });

  it("turnDuration spans first start to last end across the turn's groups", () => {
    const item = {
      kind: "execution",
      id: "x",
      turn: 1,
      settled: true,
      groups: [
        g({ startedAt: "2026-08-29T00:00:00Z", endedAt: "2026-08-29T00:00:10Z" }),
        g({
          id: "s2",
          startedAt: "2026-08-29T00:00:10Z",
          endedAt: "2026-08-29T00:00:45Z",
        }),
      ],
    } as ExecutionItem;
    expect(turnDuration(item)).toBe("45s");
    expect(turnDuration({ ...item, groups: [g()] })).toBeNull();
  });

  it("doingLine speaks the newest in-flight row, else the active group's headline", () => {
    const item = {
      kind: "execution",
      id: "x",
      turn: 1,
      settled: false,
      groups: [
        g({ state: "done" }),
        g({
          id: "s2",
          title: "Publish it",
          rows: [
            {
              t: "tool",
              id: "t1",
              tool: "web.fetch",
              summary: "github.com/qdrant/qdrant",
              pending: true,
            },
          ],
        }),
      ],
    } as ExecutionItem;
    expect(doingLine(item)).toBe("Reading github.com/qdrant/qdrant");
    const idle = { ...item, groups: [g({ id: "s2", title: "Publish it" })] };
    expect(doingLine(idle)).toBe("Publish it");
    expect(doingLine({ ...item, groups: [] }, "waiting")).toBe("Waiting for your answer");
    expect(doingLine({ ...item, groups: [] })).toBe("Getting started");
  });

  it("names an allowance wait for what it waits on - the allowance, never anyone's answer", () => {
    const item = {
      kind: "execution",
      id: "x",
      turn: 1,
      settled: false,
      groups: [],
    } as ExecutionItem;
    const allowance = { kind: "budget", budget_scope: "workspace" };
    expect(doingLine(item, "waiting", null, allowance)).toBe(
      "Spending allowance reached - waiting for it to come back",
    );
    expect(doingLine(item, "waiting", null, { kind: "budget", budget_scope: "task" })).toBe(
      "Waiting for your answer",
    );
    expect(doingLine(item, "waiting", null, { kind: "input" })).toBe("Waiting for your answer");
    expect(waitingPhrase(null)).toBe("Waiting for your answer");
    // A pause still outranks the gate's words.
    expect(doingLine(item, "waiting", { applied: true }, allowance)).toBe("Paused");
  });

  it("a task waiting on a gate says what it waits for, not the step's own headline", () => {
    const item = {
      kind: "execution",
      id: "x",
      turn: 1,
      settled: false,
      groups: [g({ id: "s1", title: "Write the summary" })],
    } as ExecutionItem;
    expect(doingLine(item)).toBe("Write the summary");
    expect(doingLine(item, "waiting", null, { kind: "input" })).toBe("Waiting for your answer");
    expect(doingLine(item, "waiting", null, { kind: "approve" })).toBe("Waiting for your approval");
    expect(doingLine(item, "waiting", null, { kind: "connect" })).toBe(
      "Waiting for you to connect",
    );
  });

  it("doingLine says Finishing up once every group settles - never a rewind to 'Getting started'", () => {
    // After the LAST step settles (and before task.delivered
    // lands) status is still `running` with no active group - the old fall-through said
    // "Getting started" into the aria-live region. Every task crosses this window.
    const wrapped = {
      kind: "execution",
      id: "x",
      turn: 1,
      settled: false,
      groups: [
        g({ id: "plan", kind: "plan", title: "", state: "done" }),
        g({ state: "done" }),
        g({ id: "s2", state: "skipped" }),
      ],
    } as ExecutionItem;
    expect(doingLine(wrapped, "running")).toBe("Finishing up");
    // The parked truths still outrank the wrap-up phrase…
    expect(doingLine(wrapped, "waiting")).toBe("Waiting for your answer");
    expect(doingLine(wrapped, "paused")).toBe("Paused");
    // …and genuinely-before-anything keeps its phrase: no groups yet, or a plan whose
    // steps have not begun.
    expect(doingLine({ ...wrapped, groups: [] }, "running")).toBe("Getting started");
    expect(
      doingLine(
        { ...wrapped, groups: [g({ state: "done" }), g({ id: "s2", state: "pending" })] },
        "running",
      ),
    ).toBe("Getting started");
  });
});

// The identity law: the agent chip's hue and the bot avatar's are the SAME
// hash - progress-copy and bot-avatar each kept a byte-identical local copy until the
// hoist into kit tone's nameHue. The alias must stay the function, and the slot a name
// lands on must never move (a hash change would repaint every chip and every avatar).
describe("the agent hue", () => {
  it("agentHueIndex IS kit's nameHue - one hash, one modulo, one pinned slot", () => {
    expect(agentHueIndex).toBe(nameHue); // the alias, not another copy
    expect(agentHueIndex("surveyor")).toBe(5);
  });
});

// Steering: a call a steer held never ran, and a pause reads as a pause - honest
// words in the tree and the tail, never the warning face of a refusal.
describe("steering in the tree's voice", () => {
  const item = (rows: ExecRow[] = []): ExecutionItem => ({
    kind: "execution",
    id: "x",
    turn: 1,
    settled: false,
    groups: [
      group({ state: "done" }),
      group({ id: "s2", title: "Draft", state: "running", agent: "writer", rows }),
    ],
  });

  it("a held call names who steered, and wears the tool's own quiet icon", () => {
    const held = tool({
      tool: "frame.shell",
      ok: false,
      summary: "",
      deniedReason: "held: Bob steered",
    });
    expect(toolLine(held)).toEqual({
      verb: "Held",
      detail: "Bob steered before this ran",
      mono: false,
    });
    expect(toolIcon(held)).not.toBe(AlertTriangle);
    expect(toolIcon(held)).toBe(toolIcon(tool({ tool: "frame.shell" })));
    expect(isHeld(held)).toBe(true);
    const two = tool({ ok: false, deniedReason: "held: Bob and Ada steered" });
    expect(toolLine(two).detail).toBe("Bob and Ada steered before this ran");
    expect(isHeld(tool({ ok: false, deniedReason: "not in the allowlist" }))).toBe(false);
  });

  it("a pause row speaks its own sentence with a pause face", () => {
    const row = tool({ tool: "pause", summary: "Paused before its next tool call", ok: true });
    expect(toolLine(row)).toEqual({
      verb: "",
      detail: "Paused before its next tool call",
      mono: false,
    });
    expect(toolIcon(row)).toBe(Pause);
  });

  it("the tail says Pausing until the pause takes hold - never the name, which the status line carries", () => {
    const running = item([tool({ tool: "frame.shell", summary: "make", pending: true })]);
    expect(doingLine(running, "paused", { by: "Ada", applied: false })).toBe("Pausing…");
    expect(doingLine(running, "paused", { by: "Ada", applied: true })).toBe("Paused");
    expect(doingLine(running, "paused", { applied: false })).toBe("Pausing…");
    expect(doingLine(running, "running", null)).toBe("Running make");
  });

  it("the root line carries the same pause words", () => {
    expect(rootLabel(item(), "paused", { by: "Ada", applied: false })).toBe(
      "Pausing… (Ada) - 1 of 2 done",
    );
    expect(rootLabel(item(), "paused", { by: "Ada", applied: true })).toBe(
      "Paused by Ada - 1 of 2 done",
    );
    expect(rootLabel(item(), "paused")).toBe("Paused - 1 of 2 done");
  });

  it("the transcript's status line: Pausing, then Paused by whom, then what to do", () => {
    expect(pauseStatusLine({ by: "Ada", applied: false })).toBe("Pausing… (Ada)");
    expect(pauseStatusLine({ by: "Ada", applied: true })).toBe(
      "Paused by Ada - resume when you want the machine to continue",
    );
    expect(pauseStatusLine(null)).toBe("paused - resume when you want the machine to continue");
  });
});

describe("a guarded step's ending on its row (agent-loop §3.2)", () => {
  it("names a partial and a sign-in block, and stays quiet otherwise", () => {
    expect(outcomeLabel(group({ outcome: "partial" }))).toBe("Partly done");
    expect(outcomeLabel(group({ state: "running", outcome: "blocked", blockedBy: "login" }))).toBe(
      "Blocked: sign-in needed",
    );
    expect(outcomeLabel(group({ state: "escalated", outcome: "blocked" }))).toBe("Blocked");
    expect(outcomeLabel(group({ outcome: "done" }))).toBeNull();
    expect(outcomeLabel(group({}))).toBeNull();
  });
});
