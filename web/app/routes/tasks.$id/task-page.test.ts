/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { describe, expect, it } from "vitest";
import type { FeedItem, StepView } from "~/lib/task-view";
import {
  answerTextOf,
  composerPlaceholder,
  earlierPlansOf,
  liveExecOf,
  liveNoteOf,
  liveNoticeOf,
  openGateOf,
  proposalKind,
  railMachineOf,
  spendOf,
  stampsPending,
  usageOf,
  railStepsOf,
  waitOf,
} from "./task-page";

const seg = (id: string, markdown: string, live = false) =>
  ({ kind: "segment", id, markdown, live }) as FeedItem;
const exec = (id: string, settled: boolean) =>
  ({ kind: "execution", id, settled }) as unknown as FeedItem;

describe("liveNoteOf", () => {
  it("says reconnecting over every stall, whoever is waited on", () => {
    for (const wait of [null, "person", "system"] as const) {
      expect(liveNoteOf(false, true, true, wait)).toBe("reconnecting…");
    }
  });

  it("an unexplained silence says waiting for signal after the normal window", () => {
    expect(liveNoteOf(true, true, false, null)).toBe("waiting for signal…");
    expect(liveNoteOf(true, false, false, null)).toBeNull();
  });

  it("a wait on a person never says waiting for signal, however long", () => {
    expect(liveNoteOf(true, true, true, "person")).toBeNull();
  });

  it("a wait on the system shows its own phrase, then the note after the long bound", () => {
    expect(liveNoteOf(true, true, false, "system")).toBeNull();
    expect(liveNoteOf(true, true, true, "system")).toBe("waiting for signal…");
  });
});

describe("waitOf", () => {
  const live = (rows: unknown[]) =>
    [
      {
        kind: "execution",
        id: "x",
        settled: false,
        groups: [{ id: "s1", kind: "step", state: "running", rows }],
      },
    ] as unknown as FeedItem[];
  const machine = { t: "tool", id: "m", tool: "machine", pending: true, waitS: 600 };

  it("a task parked on a gate or a pause waits on a person", () => {
    expect(waitOf({ status: "waiting", feed: [] })).toBe("person");
    expect(waitOf({ status: "paused", feed: [] })).toBe("person");
  });

  it("a step waiting on its Machine waits on the system; a quiet running step on nobody", () => {
    expect(waitOf({ status: "running", feed: live([machine]) })).toBe("system");
    expect(waitOf({ status: "running", feed: live([{ ...machine, pending: false }]) })).toBeNull();
    expect(waitOf({ status: "running", feed: live([]) })).toBeNull();
  });

  it("a Machine row that states no bound (an older event) is unexplained silence", () => {
    expect(
      waitOf({ status: "running", feed: live([{ ...machine, waitS: undefined }]) }),
    ).toBeNull();
  });
});

describe("liveExecOf", () => {
  it("finds the newest execution, null once it settled", () => {
    const live = exec("e2", false);
    expect(liveExecOf([exec("e1", true), seg("s", "x"), live])).toBe(live);
    expect(liveExecOf([exec("e1", false), exec("e2", true)])).toBeNull();
    expect(liveExecOf([seg("s", "x")])).toBeNull();
  });
});

describe("answerTextOf", () => {
  it("prefers the delivered summary, else the newest answer segment", () => {
    const feed = [seg("a", "first"), seg("b", "second"), seg("c", "")];
    expect(answerTextOf("done", feed)).toBe("done");
    expect(answerTextOf(undefined, feed)).toBe("second");
    expect(answerTextOf(undefined, [])).toBe("");
  });
});

describe("earlierPlansOf", () => {
  it("lists revisions then ancestors, nearest first, skipping empty plans", () => {
    const step = { id: "s" } as never;
    const plans = earlierPlansOf(
      "t",
      [[step], [step]],
      [
        { taskId: "a1", goal: "old ask", steps: [step] },
        { taskId: "a2", steps: [] },
        { taskId: "a3", steps: [step] },
      ],
    );
    expect(plans.map((p) => p.id)).toEqual(["t-rev1", "t-rev0", "a3", "a1"]);
    expect(plans[0].label).toBe("Revision 2 · replaced");
    expect(plans[2].label).toBe("Earlier task");
    expect(plans[3].label).toBe("old ask");
  });
});

describe("spend and usage", () => {
  it("reads the live ledger once it lands", () => {
    expect(spendOf({ usageUsd: 0.5, totalUsd: "0.1" })).toBe("0.5");
    expect(spendOf({ usageUsd: null, totalUsd: "0.1" })).toBe("0.1");
  });
  it("hides usage until something was spent", () => {
    expect(usageOf("0", 0, 0)).toBeNull();
    expect(usageOf("0.2", 0, 0)).toEqual({ usd: "0.2", calls: 0, searches: 0 });
    expect(usageOf("0", 3, 2)).toEqual({ usd: "0", calls: 3, searches: 2 });
  });
});

describe("railMachineOf", () => {
  it("draws the page's lease over the loader's row", () => {
    const machine = { control: { since: "s0" } } as never;
    expect(railMachineOf(null, null)).toBeNull();
    expect(railMachineOf(machine, null)?.control).toBeNull();
    const lease = { holder: { id: "u", name: "Ada" }, private: true, until: null };
    expect(railMachineOf(machine, lease)?.control).toEqual({
      holder: lease.holder,
      private: true,
      since: "s0",
      until: "",
    });
  });
});

describe("liveNoticeOf", () => {
  const base = { feed: [] as FeedItem[], draft: null, execId: null };
  it("names a held fork, a queue, a pause", () => {
    expect(liveNoticeOf({ ...base, status: "created" }, true)).toBe("held");
    expect(liveNoticeOf({ ...base, status: "created" }, false)).toBe("queued");
    expect(liveNoticeOf({ ...base, status: "paused" }, false)).toBe("paused");
  });
  it("shows the working line until text pours or the tree is up", () => {
    expect(liveNoticeOf({ ...base, status: "running" }, false)).toBe("working");
    expect(liveNoticeOf({ ...base, status: "running", feed: [seg("s", "", true)] }, false)).toBe(
      "working",
    );
    expect(
      liveNoticeOf({ ...base, status: "running", feed: [seg("s", "hi", true)] }, false),
    ).toBeNull();
    expect(
      liveNoticeOf({ ...base, status: "running", draft: { path: "/a.md", text: "x" } }, false),
    ).toBeNull();
    expect(liveNoticeOf({ ...base, status: "running", execId: "e" }, false)).toBeNull();
  });
});

describe("composerPlaceholder", () => {
  it("invites what the box will do", () => {
    expect(composerPlaceholder({ questionOpen: true, held: true, live: true })).toBe(
      "Or reply directly…",
    );
    expect(composerPlaceholder({ questionOpen: false, held: true, live: true })).toBe(
      "Where should this branch go?",
    );
    expect(composerPlaceholder({ questionOpen: false, held: false, live: true })).toBe(
      "Steer the task…",
    );
    expect(composerPlaceholder({ questionOpen: false, held: false, live: false })).toBe(
      "Ask a follow-up…",
    );
  });
});

describe("proposalKind", () => {
  it("passes only the kinds the wire takes", () => {
    expect(proposalKind("deck")).toBe("deck");
    expect(proposalKind("image")).toBeUndefined();
    expect(proposalKind(null)).toBeUndefined();
  });
});

describe("stampsPending", () => {
  const detail = { turn: 1, scale: "brief", deliverable: "document", deliverable_format: "pdf" };
  it("never asks before planning is over", () => {
    expect(stampsPending({ status: "planning", turn: 2 }, detail, false)).toBe(false);
  });
  it("asks after a newer turn, an auto size, or a missing path or format", () => {
    expect(stampsPending({ status: "running", turn: 2 }, detail, false)).toBe(true);
    expect(stampsPending({ status: "running", turn: 1 }, detail, false)).toBe(false);
    expect(stampsPending({ status: "running", turn: 1 }, { ...detail, scale: "auto" }, true)).toBe(
      true,
    );
    expect(
      stampsPending({ status: "running", turn: 1 }, { ...detail, deliverable: null }, false),
    ).toBe(true);
    expect(
      stampsPending({ status: "running", turn: 1 }, { ...detail, deliverable: null }, true),
    ).toBe(false);
    expect(
      stampsPending({ status: "running", turn: 1 }, { ...detail, deliverable_format: null }, false),
    ).toBe(true);
  });
});

describe("openGateOf", () => {
  const q = { gate_id: "g1", kind: "questions", questions: [{}], feedAt: 0 } as never;
  const c = { gate_id: "g2", kind: "control" } as never;
  const steered = {
    kind: "user",
    id: "u",
    text: "go left",
    steered: true,
    ack: { state: "accepted", line: "" },
  } as unknown as FeedItem;
  it("stages nothing on a settled task", () => {
    expect(openGateOf({ feed: [] }, [q], false)).toEqual({
      gate: null,
      questionOpen: false,
      steerNote: null,
      controlGate: null,
    });
  });
  it("stages the first gate, and notes a steer behind an open question", () => {
    const s = openGateOf({ feed: [steered] }, [q, c], true);
    expect(s.gate).toBe(q);
    expect(s.questionOpen).toBe(true);
    expect(s.steerNote).toBe("You steered while this waited; it applies after the answer");
    expect(s.controlGate).toBe(c);
  });
  it("is no question for a control gate", () => {
    const s = openGateOf({ feed: [steered] }, [c], true);
    expect(s.questionOpen).toBe(false);
    expect(s.steerNote).toBeNull();
    expect(s.controlGate).toBe(c);
  });
});

describe("railStepsOf", () => {
  const step = (id: string, state: StepView["state"]): StepView => ({
    id,
    title: id,
    state,
    attempt: 1,
  });

  it("leaves a live task's steps as they are", () => {
    const steps = [step("a", "done"), step("b", "retrying")];
    expect(railStepsOf(steps, "running")).toBe(steps);
  });

  it("a cancelled task shows no step as retrying, running or waiting to start", () => {
    const out = railStepsOf(
      [step("a", "done"), step("b", "retrying"), step("c", "running"), step("d", "ready")],
      "cancelled",
    );
    expect(out.map((s) => s.state)).toEqual(["done", "skipped", "skipped", "skipped"]);
  });

  it("keeps an escalated step's own word on a failed task", () => {
    const out = railStepsOf([step("a", "escalated"), step("b", "pending")], "failed");
    expect(out.map((s) => s.state)).toEqual(["escalated", "skipped"]);
  });
});
