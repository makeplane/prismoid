/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { ExecGroup, ExecutionItem, FeedItem, GateView } from "~/lib/task-view";
import { COLLECTION_STREAM } from "./collection-stream.fixture";
import { FeedRow } from "./route";
import { StepGroupRow, parkedOn } from "./progress-tree";
import { TaskRail } from "./task-rail";
import { ExecTail } from "./tail-line";
import { StructuredDraftCard } from "./draft-card";
import { receiptCast, receiptStepRows } from "./epilogue";
import { ControlDock } from "~/components/tasks/control-dock";
import { controlGateFor, screenPaneOpens } from "~/lib/machine-copy";

const group = (over: Partial<ExecGroup> = {}): ExecGroup => ({
  id: "s1",
  kind: "step",
  title: "Write the memo",
  state: "done",
  agent: "writer",
  attempt: 1,
  rows: [],
  ...over,
});

const execution = (over: Partial<ExecutionItem> = {}): ExecutionItem => ({
  kind: "execution",
  id: "x1",
  turn: 1,
  settled: true,
  groups: [group()],
  ...over,
});

// The user's turn speaks the universal chat grammar: a quiet secondary bubble on the right
// edge - ask, steer, and follow-up alike. Pinned so the ask-as-headline treatment (a bare
// <h2> that read as a page title, not a message) never returns.
describe("FeedRow - the user's turn", () => {
  it("wears the right-aligned secondary chat bubble", () => {
    const html = renderToStaticMarkup(
      <FeedRow
        item={{ kind: "user", id: "u1", text: "ship the Q3 board\nby friday" }}
        artifacts={[]}
        onOpenArtifact={() => {}}
      />,
    );
    expect(html).toContain('data-slot="bubble"');
    expect(html).toContain('data-align="end"');
    expect(html).toContain('data-variant="secondary"');
    expect(html).toContain("ship the Q3 board");
    expect(html).not.toContain("<h2");
  });

  it("renders the prompt as typed - pre-wrap plain text, never Markdown", () => {
    const html = renderToStaticMarkup(
      <FeedRow
        item={{ kind: "user", id: "u1", text: "*not emphasis*" }}
        artifacts={[]}
        onOpenArtifact={() => {}}
      />,
    );
    expect(html).toContain("whitespace-pre-wrap");
    expect(html).toContain("*not emphasis*"); // asterisks survive verbatim
    expect(html).not.toContain("<em>");
  });
});

describe("FeedRow - the answer's line breaks", () => {
  it("a poem in an answer segment keeps its rows", () => {
    const html = renderToStaticMarkup(
      <FeedRow
        item={{
          kind: "segment",
          id: "s1",
          agent: "lead",
          markdown: "Roses are red\nViolets are blue",
          live: false,
        }}
        artifacts={[]}
        onOpenArtifact={() => {}}
      />,
    );
    expect(html).toContain("Roses are red<br/>\nViolets are blue");
  });
});

// A failed turn's card names what the attempts reported, not only the generic advice.
describe("FeedRow - the escalation card", () => {
  const card = (evidence: string[]) =>
    renderToStaticMarkup(
      <FeedRow
        item={{
          kind: "escalation",
          id: "e1",
          headline: "Prismoid could not finish this part of the task",
          reason: "Every attempt ended before the work was finished (3 tries).",
          evidence,
          tries: ["Run it again."],
        }}
        artifacts={[]}
        onOpenArtifact={() => {}}
      />,
    );

  it("shows the reason the attempts reported under What happened", () => {
    const html = card(["no enabled model serves 'fast-executor'."]);
    expect(html).toContain("What happened");
    expect(html).toContain("no enabled model serves");
    expect(html).toContain("What to try");
  });

  it("draws no What happened section when no attempt reported anything", () => {
    expect(card([])).not.toContain("What happened");
  });
});

// Multiplayer: in a Project the timeline says WHO. The name only shows when the
// task is shared (`showAuthor`), so a personal task's transcript is byte-for-byte what it was.
describe("FeedRow - who said it", () => {
  const ada = { id: "ada-id", name: "Ada" };
  const row = (item: FeedItem, showAuthor?: boolean) =>
    renderToStaticMarkup(
      <FeedRow item={item} artifacts={[]} onOpenArtifact={() => {}} showAuthor={showAuthor} />,
    );

  it("a user turn with an author, in a shared task, names them above the bubble", () => {
    const html = row({ kind: "user", id: "u1", text: "compare them", author: ada }, true);
    expect(html).toContain("Ada");
    expect(html).toContain("asked");
    expect(html).toContain("compare them");
    expect(html.indexOf("Ada")).toBeLessThan(html.indexOf('data-slot="bubble"'));
  });

  it("a steer says steered", () => {
    const html = row(
      {
        kind: "user",
        id: "u1",
        text: "focus on pricing",
        author: { id: "b", name: "Bob" },
        steered: true,
      },
      true,
    );
    expect(html).toContain("Bob");
    expect(html).toContain("steered");
    expect(html).not.toContain("asked");
  });

  it("a personal task shows no name, even when the events carry one", () => {
    const html = row({ kind: "user", id: "u1", text: "compare them", author: ada }, false);
    expect(html).not.toContain("Ada");
    expect(html).toContain("compare them");
    expect(row({ kind: "user", id: "u2", text: "compare them", author: ada })).not.toContain("Ada");
  });

  it("an authorless turn (the optimistic paint, a legacy row) has no name line even when shared", () => {
    const html = row({ kind: "user", id: "opt-1", text: "hi", pending: true }, true);
    expect(html).not.toContain("asked");
    expect(html).toContain("opacity-70");
  });

  it("a control line renders as one muted centered line", () => {
    const html = row({ kind: "control", id: "c1", text: "Ada paused" }, true);
    expect(html).toContain("Ada paused");
    expect(html).toContain("text-muted-foreground");
    expect(html).toContain("justify-center");
    expect(html).not.toContain('data-slot="bubble"');
  });
});

// The epilogue: the end of a turn reads as one quiet sentence-shaped block -
// monochrome, the ONE accent amber on a failed verification, memories elaborated in full.
// Steering: a steer's bubble says where it stands, in one quiet line under it, and
// its author alone may take it back while it has not yet applied.
describe("FeedRow - the steer's acknowledgement", () => {
  const bob = { id: "bob-id", name: "Bob" };
  const steerItem = (ack: Extract<FeedItem, { kind: "user" }>["ack"]): FeedItem => ({
    kind: "user",
    id: "u2",
    text: "use euros",
    steered: true,
    author: bob,
    commandId: "c1",
    ack,
  });
  const row = (
    item: FeedItem,
    extra: {
      taskPaused?: boolean;
      questionOpen?: boolean;
      onRetract?: (commandId: string) => void;
      myId?: string;
    } = {},
  ) =>
    renderToStaticMarkup(
      <FeedRow item={item} artifacts={[]} onOpenArtifact={() => {}} {...extra} />,
    );
  const ACCEPTED = {
    state: "accepted" as const,
    line: "Accepted · applies at the next safe point",
  };

  it("renders the line for every state, as a status under the bubble", () => {
    for (const ack of [
      ACCEPTED,
      { state: "applied" as const, line: "Applied to Draft report before its next tool call" },
      { state: "rejected" as const, line: "Not applied: the task was cancelled first" },
      { state: "retracted" as const, line: "Retracted by Bob" },
    ]) {
      const html = row(steerItem(ack));
      expect(html).toContain(ack.line);
      expect(html).toContain('role="status"');
    }
  });

  it("an accepted steer on a paused task applies when it resumes; under an open question, after the answer", () => {
    expect(row(steerItem(ACCEPTED), { taskPaused: true })).toContain(
      "Accepted · applies when the task resumes",
    );
    expect(row(steerItem(ACCEPTED), { questionOpen: true })).toContain(
      "Accepted · applies after the question is answered",
    );
    // only an ACCEPTED steer waits: an applied one says where it took hold, paused or not
    const applied = { state: "applied" as const, line: "Applied to Draft before it finished" };
    expect(row(steerItem(applied), { taskPaused: true })).toContain(applied.line);
  });

  it("Retract shows only for the author, only while accepted, only when retracting is offered", () => {
    const onRetract = () => {};
    expect(row(steerItem(ACCEPTED), { onRetract, myId: "bob-id" })).toContain("Retract");
    expect(row(steerItem(ACCEPTED), { onRetract, myId: "ada-id" })).not.toContain(">Retract<");
    expect(row(steerItem(ACCEPTED), { myId: "bob-id" })).not.toContain(">Retract<");
    const applied = { state: "applied" as const, line: "Applied to Draft before it finished" };
    expect(row(steerItem(applied), { onRetract, myId: "bob-id" })).not.toContain(">Retract<");
  });

  it("an ask (not a steer) draws no acknowledgement line", () => {
    const html = row({ kind: "user", id: "u1", text: "write a memo" });
    expect(html).not.toContain('role="status"');
  });
});

describe("FeedRow - a superseded artifact (ADR-064)", () => {
  it("a re-run's superseded card is greyed and names whose steer replaced it", () => {
    const item: FeedItem = { kind: "artifact", id: "a1", artifactId: "art-1", title: "Report" };
    const artifacts = [{ id: "art-1", title: "Report", type: "document" }];
    const html = renderToStaticMarkup(
      <FeedRow
        item={item}
        artifacts={artifacts}
        onOpenArtifact={() => {}}
        superseded={{ "art-1": "Bob" }}
        showAuthor
      />,
    );
    expect(html).toContain("opacity-60");
    expect(html).toContain("Superseded by Bob&#x27;s steer");
    expect(html).not.toContain(">Document<"); // the superseded line replaces the kind, never both
    const fresh = renderToStaticMarkup(
      <FeedRow item={item} artifacts={artifacts} onOpenArtifact={() => {}} superseded={{}} />,
    );
    expect(fresh).not.toContain("Superseded");
    expect(fresh).not.toContain("opacity-60");
  });

  it("on a personal task the card names no one - the only one steering is you", () => {
    const item: FeedItem = { kind: "artifact", id: "a1", artifactId: "art-1", title: "Report" };
    const html = renderToStaticMarkup(
      <FeedRow
        item={item}
        artifacts={[{ id: "art-1", title: "Report", type: "document" }]}
        onOpenArtifact={() => {}}
        superseded={{ "art-1": "Bob" }}
      />,
    );
    expect(html).toContain("Superseded by a steer");
    expect(html).not.toContain("Bob");
  });
});

describe("TaskRail - the shelf (ADR-064)", () => {
  it("a superseded artifact is not on the shelf at all", () => {
    const html = renderToStaticMarkup(
      <TaskRail
        steps={[]}
        artifacts={[
          { id: "old", title: "Report v1", type: "document" },
          { id: "new", title: "Report v2", type: "document" },
        ]}
        onOpenArtifact={() => {}}
        superseded={{ old: "Bob" }}
      />,
    );
    expect(html).toContain("Report v2");
    expect(html).not.toContain("Report v1");
    expect(html).toContain("· 1"); // the count is the shelf's, not the record's
  });
});

describe("FeedRow - the epilogue", () => {
  it("delivered + failed verification + memories: one quiet block, amber rule, no red", () => {
    const html = renderToStaticMarkup(
      <FeedRow
        item={{
          kind: "epilogue",
          id: "ep1",
          delivered: true,
          verdict: "fail",
          suite: "document-suite",
          family: "google",
          artifactId: "a1",
          remembered: [
            { kind: "preference", text: "Prefers tables over prose." },
            { kind: "fact", text: "Ships a phone hardware project." },
          ],
        }}
        artifacts={[]}
        onOpenArtifact={() => {}}
      />,
    );
    expect(html).toContain("Delivered");
    expect(html).toContain("Checks found problems. Read it with care.");
    expect(html).toContain("See the receipt");
    expect(html).toContain("text-warning"); // amber: the caveat and the rule beside it
    expect(html).toContain("border-warning");
    expect(html).not.toContain("text-destructive"); // never red here
    expect(html).not.toContain("text-success"); // and never green
    expect(html).toContain("Remembered 2 things");
    // Folded by default - the items live behind the disclosure, not in the first paint.
    expect(html).not.toContain("Prefers tables over prose.");
  });

  it("the turn receipt joins the delivery sentence: steps · agents · time · tokens (round seven)", () => {
    const html = renderToStaticMarkup(
      <FeedRow
        item={{
          kind: "epilogue",
          id: "ep1",
          delivered: true,
          remembered: [],
          receipt: { steps: 14, agents: 4, durationMs: 392_000, tokens: 812_400 },
        }}
        artifacts={[]}
        onOpenArtifact={() => {}}
      />,
    );
    expect(html).toContain("Delivered");
    expect(html).toContain("14 steps");
    expect(html).toContain("4 agents");
    expect(html).toContain("in 6m 32s"); // the time joins "Delivered"
    expect(html).toContain("812.4k tokens");
    // The countable bits are x-rays - steps, agents and the token total (it opens into
    // fresh and cached) wear the dotted underline; the time stays a plain fact.
    expect(html.match(/decoration-dotted/g)).toHaveLength(3);
  });

  it("the x-ray builders tell the story behind the numbers (hover, round seven)", () => {
    const receipt = {
      steps: 3,
      agents: 3,
      rows: [
        { title: "Planning", agent: "planner", kind: "plan" as const, state: "done" as const },
        {
          title: "01 Recommendation",
          agent: "section_writer",
          kind: "step" as const,
          state: "done" as const,
          durationMs: 9_500,
          tokens: 8_200,
        },
        {
          title: "02 Operational Burden",
          agent: "section_writer",
          kind: "step" as const,
          state: "skipped" as const,
          tokens: 1_000,
        },
        {
          title: "Assemble",
          agent: "typesetter",
          kind: "step" as const,
          state: "done" as const,
          durationMs: 197_000,
          tokens: 742_400,
        },
      ],
    };
    const rows = receiptStepRows(receipt);
    // Step rows only, feed order, each with its meta - the plan phase stays out (it is
    // not a step; the cast rollup carries the planner).
    expect(rows.map((r) => r.title)).toEqual([
      "01 Recommendation",
      "02 Operational Burden",
      "Assemble",
    ]);
    expect(rows[0]).toMatchObject({ agent: "section_writer", meta: "9.5s · 8.2k tok" });
    expect(rows[1].offNominal).toBe("skipped");
    expect(rows[2].meta).toBe("3m 17s · 742.4k tok");

    const cast = receiptCast(receipt);
    expect(cast.map((c) => [c.agent, c.meta])).toEqual([
      ["planner", "planning"],
      ["section_writer", "2 steps · 9.2k tok"],
      ["typesetter", "1 step · 742.4k tok"],
    ]);
  });

  it("no receipt (a legacy replay) keeps the bare Delivered line - never zeros", () => {
    const html = renderToStaticMarkup(
      <FeedRow
        item={{ kind: "epilogue", id: "ep1", delivered: true, remembered: [] }}
        artifacts={[]}
        onOpenArtifact={() => {}}
      />,
    );
    expect(html).toContain("Delivered");
    expect(html).not.toContain("0 steps");
    expect(html).not.toContain("tok");
  });

  it("a clean pass says Verified with its suite on a green rule, and never amber", () => {
    const html = renderToStaticMarkup(
      <FeedRow
        item={{
          kind: "epilogue",
          id: "ep1",
          delivered: true,
          verdict: "pass",
          suite: "document-suite",
          family: "google",
          remembered: [],
        }}
        artifacts={[]}
        onOpenArtifact={() => {}}
      />,
    );
    expect(html).toContain("Delivered");
    expect(html).toContain("Verified · document-suite · judged by google");
    expect(html).toContain("border-success"); // the rule's colour is the verdict
    expect(html).not.toContain("text-warning");
    expect(html).not.toContain("border-warning");
  });
});

// The execution tree: settled = one quiet line, expanded on demand; live =
// the active group open with its pending activity breathing. Pinned at the render layer
// because the collapse DEFAULTS are component behavior the reducer cannot see.
describe("FeedRow - the execution tree", () => {
  it("a settled tree collapses to the one-line summary - no group content in the DOM", () => {
    const html = renderToStaticMarkup(
      <FeedRow
        item={execution({
          groups: [group(), group({ id: "s2", title: "Publish it", state: "done" })],
        })}
        artifacts={[]}
        onOpenArtifact={() => {}}
      />,
    );
    expect(html).toContain("Completed 2 steps");
    expect(html).not.toContain("Write the memo"); // folded away until expanded
  });

  it("the settled one-liner keeps the receipt - time and tokens survive settlement", () => {
    const html = renderToStaticMarkup(
      <FeedRow
        item={execution({
          groups: [
            group({
              startedAt: "2026-08-29T00:00:00Z",
              endedAt: "2026-08-29T00:00:20Z",
              tokens: 6400,
            }),
            group({
              id: "s2",
              state: "done",
              startedAt: "2026-08-29T00:00:20Z",
              endedAt: "2026-08-29T00:00:45Z",
              tokens: 104_400,
            }),
          ],
        })}
        artifacts={[]}
        onOpenArtifact={() => {}}
      />,
    );
    expect(html).toContain("Completed 2 steps");
    expect(html).toContain("45s · 110.8k tok");
  });

  it("WHO is working is first-class: agent chips on every group, the worker in the root", () => {
    const html = renderToStaticMarkup(
      <FeedRow
        item={execution({
          settled: false,
          groups: [
            group({ id: "plan", kind: "plan", title: "", agent: "planner" }),
            group({ id: "s1", state: "running", agent: "writer" }),
          ],
        })}
        artifacts={[]}
        onOpenArtifact={() => {}}
      />,
    );
    expect(html).toContain(">writer<"); // the step group's agent chip - on the trigger, never folded away
    expect(html).toContain(">planner<"); // the plan group's too
    // The working line lives at ROUTE level now (ExecTail, round five) - the tree's
    // live face is purely the story: no root header, no tail of its own.
    expect(html).not.toContain("Working - step");
  });

  it("during intake/planning the PLAN group is the show - its machinery actors visible", () => {
    // Before any step exists, the phase rows are the only thing happening; the plan
    // group auto-opens so "Clarifier · Reviewing your request" is never invisible.
    const html = renderToStaticMarkup(
      <FeedRow
        item={execution({
          settled: false,
          groups: [
            group({
              id: "plan",
              kind: "plan",
              title: "",
              agent: "planner",
              state: "running",
              rows: [
                {
                  t: "tool",
                  id: "p1",
                  tool: "clarifier",
                  summary: "Reviewing your request",
                  pending: true,
                },
              ],
            }),
          ],
        })}
        artifacts={[]}
        onOpenArtifact={() => {}}
      />,
    );
    expect(html).toContain("Clarifier ·");
    expect(html).toContain("Reviewing your request");
  });

  it("a live tree opens the ACTIVE group; a pending tool row wears the spinner", () => {
    const html = renderToStaticMarkup(
      <FeedRow
        item={execution({
          settled: false,
          groups: [
            group({ state: "done" }),
            group({
              id: "s2",
              title: "Publish it",
              state: "running",
              rows: [
                {
                  t: "tool",
                  id: "tc1",
                  tool: "frame.read_file",
                  summary: "/task/inputs/brief.md",
                  pending: true,
                },
              ],
            }),
          ],
        })}
        artifacts={[]}
        onOpenArtifact={() => {}}
      />,
    );
    // The tail line: mark · time · tokens · doing - here the fixture has no
    // clock/ledger, so the newest in-flight row IS the line.
    expect(html).toContain("Reading"); // the live verb on the pending row
    expect(html).toContain("/task/inputs/brief.md");
    expect(html).toContain("animate-liveness"); // the fade trio on the pending row
  });

  it("the connection note survives a parked task - waiting must not hide a dropped socket", () => {
    // The regression: the liveNote call sites once nulled the note on
    // `waiting`, so a task parked at a gate showed no "reconnecting…" when the socket
    // dropped. The tail (ExecTail, round five: lifted to the route so it sits below a
    // streaming draft card too) is the ONE surface carrying connection truth.
    const html = renderToStaticMarkup(
      <ExecTail
        item={execution({
          settled: false,
          groups: [group({ state: "running" })],
        })}
        live={null}
        status="waiting"
        note="reconnecting…"
      />,
    );
    expect(html).toContain("reconnecting…");
  });

  it("the tail names the pouring draft, sums the turn, and vanishes at settle", () => {
    const item = execution({
      settled: false,
      groups: [
        group({
          state: "running",
          startedAt: "2026-08-29T00:00:00Z",
          tokens: 8200,
        }),
      ],
    });
    const html = renderToStaticMarkup(
      <ExecTail item={item} live={null} draftName="launch-memo.md" />,
    );
    expect(html).toContain("Drafting launch-memo.md");
    expect(html).toContain("8.2k tok");
    expect(html).toContain("animate-liveness");
    expect(renderToStaticMarkup(<ExecTail item={{ ...item, settled: true }} live={null} />)).toBe(
      "",
    );
  });

  it("a call a steer held reads Held, quietly - never the warning face", () => {
    const html = renderToStaticMarkup(
      <FeedRow
        item={execution({
          settled: false,
          groups: [
            group({
              state: "running",
              rows: [
                {
                  t: "tool",
                  id: "tc1",
                  tool: "frame.shell",
                  summary: "",
                  ok: false,
                  deniedReason: "held: Bob steered",
                },
              ],
            }),
          ],
        })}
        artifacts={[]}
        onOpenArtifact={() => {}}
      />,
    );
    expect(html).toContain("Held");
    expect(html).toContain("Bob steered before this ran");
    expect(html).not.toContain("text-warning");
  });

  it("the tail says Pausing until the pause takes hold - the name lives on the status line only", () => {
    const item = execution({ settled: false, groups: [group({ state: "running" })] });
    const tail = (pause: { by?: string; applied: boolean }) =>
      renderToStaticMarkup(<ExecTail item={item} live={null} status="paused" pause={pause} />);
    expect(tail({ by: "Ada", applied: false })).toContain("Pausing…");
    expect(tail({ by: "Ada", applied: true })).toContain("Paused");
    expect(tail({ by: "Ada", applied: true })).not.toContain("Ada");
  });

  it("a refused tool row is one of the tree's few loud lines", () => {
    const html = renderToStaticMarkup(
      <FeedRow
        item={execution({
          settled: false,
          groups: [
            group({
              state: "running",
              rows: [
                {
                  t: "tool",
                  id: "tc1",
                  tool: "frame.egress",
                  summary: "host=pypi.org",
                  ok: false,
                  deniedReason: "not in the allowlist",
                },
              ],
            }),
          ],
        })}
        artifacts={[]}
        onOpenArtifact={() => {}}
      />,
    );
    expect(html).toContain("Blocked"); // the proxy refused it - nobody "denied" anything
    expect(html).toContain("not in the allowlist");
    expect(html).toContain("text-warning");
  });

  it("rows past six clamp behind +N more", () => {
    const rows = Array.from({ length: 9 }, (_, i) => ({
      t: "tool" as const,
      id: `tc${i}`,
      tool: "frame.read_file",
      summary: `/task/inputs/f${i}.md`,
      ok: true,
    }));
    const html = renderToStaticMarkup(
      <FeedRow
        item={execution({
          settled: false,
          groups: [group({ state: "running", rows })],
        })}
        artifacts={[]}
        onOpenArtifact={() => {}}
      />,
    );
    expect(html).toContain("+3 more");
  });

  it("a SETTLED tree claims no liveness - interrupted rows keep their words, lose their motion", () => {
    // A cancel mid-step leaves `running` group states and pending rows behind (no closing
    // frame ever arrives). Expanding the settled snapshot must show a still record: no
    // spinner, no status ping, no ticking clock - the words ("Reading …") stay, honestly
    // naming what was happening when the turn stopped.
    const interrupted = group({
      state: "running",
      rows: [
        { t: "tool", id: "tc1", tool: "frame.read_file", summary: "/w/a.md", pending: true },
        { t: "round", id: "r-s1", pending: true },
      ],
    });
    const settled = renderToStaticMarkup(
      <StepGroupRow group={interrupted} settled open onOpenChange={() => {}} />,
    );
    expect(settled).not.toContain("animate-liveness");
    expect(settled).not.toContain("animate-status-ping");
    expect(settled).toContain("/w/a.md"); // the interrupted row's words survive
    const live = renderToStaticMarkup(
      <StepGroupRow group={interrupted} settled={false} open onOpenChange={() => {}} />,
    );
    expect(live).toContain("animate-liveness"); // the same rows DO march while live
  });

  it("only the step holding the open gate is parked; a parallel sibling keeps working", () => {
    const gate = { t: "tool" as const, id: "gate-g1", tool: "gate", pending: true };
    const item = execution({
      groups: [
        group({ id: "s1", state: "running", rows: [gate] }),
        group({ id: "s2", state: "running", rows: [] }),
      ],
    });
    const parked = parkedOn(item, "waiting");
    expect(parked("s1")).toBe(true);
    expect(parked("s2")).toBe(false);
    expect(parkedOn(item, "running")("s1")).toBe(false);
    // a plan-level gate (no step holds it) parks every active step
    const planGate = execution({ groups: [group({ id: "s2", state: "running" })] });
    expect(parkedOn(planGate, "waiting")("s2")).toBe(true);
  });

  it("a step parked on a gate wears the waiting dot, not running", () => {
    const g = group({ state: "running" });
    const dot = (parked: boolean) =>
      renderToStaticMarkup(
        <StepGroupRow group={g} settled={false} parked={parked} open onOpenChange={() => {}} />,
      );
    expect(dot(false)).toContain('aria-label="Status: running"');
    expect(dot(true)).toContain('aria-label="Status: waiting"');
    // the empty body's "working…" shimmer is not claimed while the task waits on a person
    expect(dot(false)).toContain("working…");
    expect(dot(true)).not.toContain("working…");
    // a finished step is never re-labelled by the task's wait
    const done = renderToStaticMarkup(
      <StepGroupRow
        group={group({ state: "done" })}
        settled={false}
        parked
        open
        onOpenChange={() => {}}
      />,
    );
    expect(done).toContain('aria-label="Status: done"');
  });

  it("two tiers (2026-08-29): the title owns line one, the receipt sits beneath it", () => {
    const g = group({
      title: "Map the territory",
      agent: "surveyor",
      // provider-neutral fixture: real model strings live under ai/**/providers only
      modelRef: "example-large-v1",
      calls: 8,
      tokens: 110_800,
      costUsd: 0.024,
      startedAt: "2026-08-29T00:00:00Z",
      endedAt: "2026-08-29T00:00:30Z",
    });
    const html = renderToStaticMarkup(
      <StepGroupRow group={g} settled open={false} onOpenChange={() => {}} />,
    );
    // every datum survives: model (mono, tooltipped), calls, tokens, cost, duration
    expect(html).toContain("example-large-v1");
    expect(html).toContain('title="example-large-v1"');
    expect(html).toContain("font-mono");
    expect(html).toContain("8 model calls");
    expect(html).toContain("110.8k tok");
    expect(html).toContain("$0.024");
    expect(html).toContain("30s");
    expect(html).toContain("tabular-nums");
    // the title keeps its own flexible line - it no longer shares width with the receipt
    expect(html).toContain("Map the territory");
    expect(html).toContain("flex-1 truncate");
  });

  it("a step names every model it used, and its cached tokens beside the fresh ones", () => {
    const g = group({
      title: "Write",
      models: ["example-large-v1", "example-small-v1"],
      calls: 6,
      searches: 2,
      tokens: 22,
      cachedTokens: 12_800,
      costUsd: 0.04,
    });
    const html = renderToStaticMarkup(
      <StepGroupRow group={g} settled open={false} onOpenChange={() => {}} />,
    );
    expect(html).toContain("example-large-v1, example-small-v1");
    expect(html).toContain("6 model calls · 2 searches");
    expect(html).toContain("22 tok + 12.8k cached");
  });

  it("the agent chip wears its ramp hue, deterministically", () => {
    const html = renderToStaticMarkup(
      <StepGroupRow
        group={group({ agent: "surveyor" })}
        settled
        open={false}
        onOpenChange={() => {}}
      />,
    );
    // The hue rides a Propel label variant (the mono-caps tag treatment went with
    // className); the DOM keeps the real lowercase id (screen readers, copy).
    expect(html).toMatch(/bg-(label-\w+-bg|info-subtle|success-subtle)/);
    expect(html).toContain(">surveyor<");
    const again = renderToStaticMarkup(
      <StepGroupRow
        group={group({ agent: "surveyor" })}
        settled
        open={false}
        onOpenChange={() => {}}
      />,
    );
    expect(again).toBe(html); // same agent, same hue - identity, not decoration
  });
});

// The collection draft's live breakdown ("building - N rows" alone answered
// nothing while a complex artifact streamed): the organizer's own file name replaces the
// scratch "draft" name, the title headlines, the summary explains, columns land one by
// one, the row tail shows what just landed - all read from the incomplete JSON, never
// parsed.
describe("StructuredDraftCard - the live breakdown", () => {
  it("shows the whole story as it streams - real name, title, summary, columns, tail, views", () => {
    const html = renderToStaticMarkup(
      <StructuredDraftCard path="/work/draft.collection.json" text={COLLECTION_STREAM} />,
    );
    expect(html).toContain("launch-tracker.collection.json"); // the real name, not "draft"
    expect(html).not.toContain(">draft.collection.json<");
    expect(html).toContain("Launch tracker");
    expect(html).toContain("Your launches, organized in one board.");
    expect(html).toContain("2 columns - Name · Stage");
    expect(html).toContain("2 rows so far");
    expect(html).toContain("→ Q3 launch email"); // the newest row wears the arrow…
    expect(html).not.toContain("→ Metro-North"); // …older tail rows sit above it, arrowless
    expect(html.match(/→ /g)).toHaveLength(1); // exactly one arrow - one newest
    expect(html).toContain("Views: Board · Table");
  });

  it("before anything parses it stays the quiet building line - no empty body", () => {
    const html = renderToStaticMarkup(
      <StructuredDraftCard path="/work/draft.collection.json" text={'{"filename": "launch-tr'} />,
    );
    expect(html).toContain("draft.collection.json"); // the scratch name, until the real one closes
    expect(html).toContain("building…");
    expect(html).not.toContain("border-t"); // the breakdown body has not opened
  });
});

// The device: the page never opens the screen pane for a
// device Machine - its browser is a window on the user's own computer, with no screen relay -
// and the CONTROL dock it mounts there offers Done, never "Open the screen".
describe("a device task's screen", () => {
  const device = {
    state: "parked",
    profile: "browser",
    driver: "device" as const,
    device_name: "MacBook Pro",
  };
  const cloud = { state: "parked", profile: "browser", driver: null };
  const handoff: GateView = {
    gate_id: "g1",
    kind: "control",
    source: "handoff",
    title: "The agent needs someone to sign in",
    questions: [],
  };

  it("never opens the screen pane, even when asked", () => {
    expect(screenPaneOpens(true, device)).toBe(false);
    expect(screenPaneOpens(true, cloud)).toBe(true);
    expect(screenPaneOpens(true, null)).toBe(false);
    expect(screenPaneOpens(false, cloud)).toBe(false);
  });

  it("mounts the CONTROL dock without Open the screen", () => {
    const dock = (machine: typeof device | typeof cloud) =>
      renderToStaticMarkup(
        <ControlDock
          gate={controlGateFor(handoff, machine)}
          canEdit
          isCreator
          deviceName="MacBook Pro"
          onOpenScreen={() => {}}
        />,
      );
    expect(dock(device)).not.toContain("Open the screen");
    expect(dock(device)).toContain(">Done<");
    expect(dock(cloud)).toContain(">Open the screen<");
  });
});
