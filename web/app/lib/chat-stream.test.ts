/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { describe, expect, it } from "vitest";
import {
  type ChatLive,
  type MessageOut,
  cardSettle,
  cardsOf,
  documentShaped,
  foldLive,
  foldMessage,
  foldMessages,
  initialChat,
  replyPending,
} from "./chat-stream";

const at = "2026-09-02T10:00:00Z";
const conversation_id = "11111111-1111-4111-8111-111111111111";

function row(seq: number, role: MessageOut["role"], over: Partial<MessageOut> = {}): MessageOut {
  return {
    seq,
    role,
    kind: "text",
    text: `line ${seq}`,
    sender: role === "user" ? "owner" : "scribe",
    verdict: role === "user" ? "answer" : "none",
    verdict_reason: "",
    consent: "none",
    status: "sent",
    detail: "",
    task_id: null,
    gate_id: null,
    artifact_ids: [],
    proposal: null,
    meta: {},
    at,
    ...over,
  };
}

function live(frame: Partial<ChatLive> & { type: ChatLive["type"] }): ChatLive {
  return {
    lane: "live",
    conversation_id,
    after_seq: 1,
    at,
    ...frame,
  } as ChatLive;
}

describe("the conversation fold (ADR-059 §5)", () => {
  it("folds durable rows once, in seq order, and keeps the poll cursor", () => {
    let s = initialChat();
    s = foldMessages(s, [row(2, "bot"), row(1, "user")]);
    s = foldMessage(s, row(2, "bot")); // a replay overlap is free to discard
    expect(s.rows.map((r) => r.seq)).toEqual([1, 2]);
    expect(s.lastSeq).toBe(2);
  });

  it("streams the reply: start opens, deltas append, activity narrates, end marks", () => {
    let s = foldMessage(initialChat(), row(1, "user"));
    expect(replyPending(s)).toBe(true);
    s = foldLive(s, live({ type: "chat.start", bot: "scribe" }));
    s = foldLive(
      s,
      live({
        type: "chat.activity",
        phase: "start",
        tool: "web.search",
        label: "web.search",
      }),
    );
    s = foldLive(s, live({ type: "chat.delta", text: "Karl " }));
    s = foldLive(s, live({ type: "chat.activity", phase: "end", ok: true }));
    s = foldLive(s, live({ type: "chat.delta", text: "Schwarzschild." }));
    s = foldLive(s, live({ type: "chat.end" }));
    expect(s.live).toEqual({
      bot: "scribe",
      text: "Karl Schwarzschild.",
      tools: [{ id: "", tool: "web.search", label: "web.search", done: true, ok: true }],
      ended: true,
    });
  });

  it("a delta with no start still opens a bubble (a reconnect may join mid-reply)", () => {
    const s = foldLive(initialChat(), live({ type: "chat.delta", text: "mid-" }));
    expect(s.live?.text).toBe("mid-");
  });

  it("the durable bot row retires the bubble - the stream's terminal is on the record", () => {
    let s = foldMessage(initialChat(), row(1, "user"));
    s = foldLive(s, live({ type: "chat.delta", text: "hello" }));
    s = foldMessage(s, row(2, "bot", { text: "hello" }));
    expect(s.live).toBeNull();
    expect(replyPending(s)).toBe(false);
  });

  it("offers the one tap only for a reply that looks like a file (ADR-060 §5)", () => {
    expect(documentShaped("Karl Schwarzschild, 1916.")).toBe(false);
    expect(documentShaped("# Memo\n\nOne paragraph.")).toBe(false); // a title alone is a reply
    expect(documentShaped("# Renewals memo\n\n## Decisions\n\ntext\n\n## Actions\n\ntext")).toBe(
      true,
    );
    expect(documentShaped("words ".repeat(600))).toBe(true);
  });

  it("a task's rows fold into one card, placed where the task started (ADR-060 §2)", () => {
    const t = "22222222-2222-4222-8222-222222222222";
    const rows: MessageOut[] = [
      row(1, "user"),
      row(2, "system", {
        kind: "started",
        task_id: t,
        text: "Started a task: write the notes",
      }),
      row(3, "system", {
        kind: "gate",
        task_id: t,
        gate_id: "g1",
        text: "Allow 1 action?",
      }),
      row(4, "system", { kind: "delivered", task_id: t, artifact_ids: ["a1"] }),
      row(5, "user"),
      row(6, "system", {
        kind: "continued",
        task_id: t,
        text: "Continued the task (turn 2).",
      }),
      row(7, "bot", { task_id: t, text: "Shorter now.", artifact_ids: ["a2"] }),
      row(8, "system", { kind: "failed", text: "scribe could not reply" }), // no task: not a card
    ];
    const cards = cardsOf(rows);
    expect(cards.firstSeq.get(t)).toBe(2);
    expect(cards.byTask.get(t)?.map((r) => r.kind)).toEqual([
      "started",
      "gate",
      "delivered",
      "continued",
    ]);
    expect(cards.byTask.size).toBe(1);
  });

  it("a late tail of a reply already on the record never re-opens a bubble", () => {
    // the found-live shape: the row's commit notification beat the batch
    // notifier's last flush, and "like me to do?" appeared as a second bubble
    let s = foldMessage(initialChat(), row(1, "user"));
    s = foldLive(s, live({ type: "chat.delta", text: "What would you ", after_seq: 1 }));
    s = foldMessage(s, row(2, "bot", { text: "What would you like me to do?" }));
    s = foldLive(s, live({ type: "chat.delta", text: "like me to do?", after_seq: 1 }));
    s = foldLive(s, live({ type: "chat.end", after_seq: 1 }));
    expect(s.live).toBeNull();
    // ...while the NEXT reply, anchored past that row, still streams
    s = foldMessage(s, row(3, "user"));
    s = foldLive(s, live({ type: "chat.delta", text: "Sure.", after_seq: 3 }));
    expect(s.live?.text).toBe("Sure.");
  });
});

describe("found in review: the terminal rule, edits in place, the card's last turn", () => {
  it("a failed or stalled reply (a SYSTEM line, no task) retires the bubble", () => {
    let s = foldMessage(initialChat(), row(1, "user"));
    s = foldLive(s, live({ type: "chat.delta", text: "half a re" }));
    s = foldMessage(s, row(2, "system", { kind: "failed", text: "scribe could not reply" }));
    expect(s.live).toBeNull();
    expect(s.settledSeq).toBe(2);
  });

  it("a bot line that carries a task (a continued turn coming home) is not the reply's end", () => {
    const t = "22222222-2222-4222-8222-222222222222";
    let s = foldMessage(initialChat(), row(1, "user"));
    s = foldLive(s, live({ type: "chat.delta", text: "Karl ", after_seq: 1 }));
    s = foldMessage(s, row(2, "bot", { task_id: t, text: "Shorter now." }));
    expect(s.live?.text).toBe("Karl ");
    s = foldLive(s, live({ type: "chat.delta", text: "Schwarzschild.", after_seq: 1 }));
    expect(s.live?.text).toBe("Karl Schwarzschild.");
  });

  it("a known seq again is an edit: replaced in place, nothing else moves", () => {
    let s = foldMessages(initialChat(), [row(1, "user"), row(2, "bot")]);
    s = foldLive(s, live({ type: "chat.delta", text: "next", after_seq: 2 }));
    s = foldMessage(s, row(1, "user", { status: "answered", task_id: null }));
    expect(s.rows[0]?.status).toBe("answered");
    expect(s.rows.length).toBe(2);
    expect(s.lastSeq).toBe(2);
    expect(s.live?.text).toBe("next");
  });

  it("activity ends pair by call id, not by recency", () => {
    let s = foldLive(initialChat(), live({ type: "chat.start", bot: "scribe" }));
    const start = (id: string, tool: string) =>
      live({ type: "chat.activity", phase: "start", tool, label: tool, tool_call_id: id } as never);
    const end = (id: string, ok: boolean) =>
      live({ type: "chat.activity", phase: "end", ok, tool_call_id: id } as never);
    s = foldLive(s, start("a", "web.fetch"));
    s = foldLive(s, start("b", "web.search"));
    s = foldLive(s, end("a", false));
    expect(s.live?.tools.map((t) => [t.tool, t.done, t.ok])).toEqual([
      ["web.fetch", true, false],
      ["web.search", false, null],
    ]);
  });

  it("a card settles by its LAST turn, and a reply under it re-opens it", () => {
    const t = "22222222-2222-4222-8222-222222222222";
    const rows: MessageOut[] = [
      row(2, "system", { kind: "started", task_id: t }),
      row(4, "system", { kind: "delivered", task_id: t }),
      row(6, "system", { kind: "continued", task_id: t }),
    ];
    let cards = cardsOf(rows);
    expect(cardSettle(cards.byTask.get(t)!, cards.botSeq.get(t))).toEqual({
      settled: false,
      row: null,
    });
    // the continued turn comes home as the bot's own line
    rows.push(row(7, "bot", { task_id: t, text: "Shorter now." }));
    cards = cardsOf(rows);
    expect(cardSettle(cards.byTask.get(t)!, cards.botSeq.get(t)).settled).toBe(true);
    // ...or fails, as a card row of its own
    const failed = [...rows.slice(0, 3), row(8, "system", { kind: "failed", task_id: t })];
    cards = cardsOf(failed);
    expect(cardSettle(cards.byTask.get(t)!, cards.botSeq.get(t)).row?.kind).toBe("failed");
    // a cancel is a card kind too
    const cancelled = [rows[0]!, row(3, "system", { kind: "cancelled", task_id: t })];
    cards = cardsOf(cancelled);
    expect(cardSettle(cards.byTask.get(t)!, undefined).row?.kind).toBe("cancelled");
  });
});
