/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The conversation stream's fold (ADR-059) - pure, so the reply's arrival is a table test.
//
// Two lanes, one socket, the task stream's law one table over: `message` frames are the
// durable rows (id: seq - the browser's own reconnect cursor), `chat.*` frames are the reply
// as it is written (no id, anchored on after_seq). Every live stream ends in a durable row:
// the bot's line, or the SYSTEM line that says the reply could not be written - either
// retires the live bubble, because what streamed is now on the record (or never will be),
// and a frame lost in a disconnect never mattered.

import type { components } from "~/lib/api/types.gen";

export type MessageOut = components["schemas"]["MessageOut"];
export type ChatStart = components["schemas"]["ChatStart"];
export type ChatDelta = components["schemas"]["ChatDelta"];
export type ChatActivity = components["schemas"]["ChatActivity"];
export type ChatEnd = components["schemas"]["ChatEnd"];
export type ChatLive = ChatStart | ChatDelta | ChatActivity | ChatEnd;

/** The SSE event names the hook listens for - `message` is Lane 1, the rest Lane 2. */
export const CHAT_EVENT_TYPES = [
  "message",
  "chat.start",
  "chat.delta",
  "chat.activity",
  "chat.end",
] as const;

export type LiveTool = {
  /** The call id the wire pairs `end` with `start` by (tools run in parallel). */
  id: string;
  tool: string;
  label: string;
  done: boolean;
  ok: boolean | null;
};

/** The reply in flight: the bot's name, its text so far, the tools it is using, and whether
 *  the wire said end (the durable row is then moments away). */
export type LiveReply = {
  bot: string;
  text: string;
  tools: LiveTool[];
  ended: boolean;
};

export type ChatState = {
  rows: MessageOut[];
  /** The newest durable seq folded - the poll fallback's `since` cursor. */
  lastSeq: number;
  /** The newest seq that TERMINATED a reply: every live frame anchored before it belongs to
   *  a reply that is already on the record, and is dropped (the reconciliation rule). */
  settledSeq: number;
  live: LiveReply | null;
};

export function initialChat(): ChatState {
  return { rows: [], lastSeq: 0, settledSeq: 0, live: null };
}

/** The row that ends a streamed reply: the BOT's own line (never a bot line that carries a
 *  task - that is a continued task's delivery coming home, a message of its own), or the
 *  SYSTEM line that says the reply failed or stalled (no task either). */
function terminatesReply(row: MessageOut): boolean {
  if (row.task_id) return false;
  return row.role === "bot" || (row.role === "system" && row.kind === "failed");
}

/** A durable row: a known seq is an EDIT of a row already folded (a card started, a line
 *  answered or failed, a memory filed) and REPLACES it in place; a new seq keeps seq order,
 *  and a row that terminates the reply retires the live bubble. */
export function foldMessage(state: ChatState, row: MessageOut): ChatState {
  const at = state.rows.findIndex((r) => r.seq === row.seq);
  if (at >= 0) {
    const rows = state.rows.slice();
    rows[at] = row;
    return { ...state, rows };
  }
  const rows = [...state.rows, row].sort((a, b) => a.seq - b.seq);
  const terminal = terminatesReply(row);
  return {
    rows,
    lastSeq: Math.max(state.lastSeq, row.seq),
    settledSeq: terminal ? Math.max(state.settledSeq, row.seq) : state.settledSeq,
    live: terminal ? null : state.live,
  };
}

/** Several rows at once (the replay, a poll page) - the same law, row by row. */
export function foldMessages(state: ChatState, rows: MessageOut[]): ChatState {
  return rows.reduce(foldMessage, state);
}

/** A live frame: start opens the bubble, deltas append (never rewrite), activity narrates a
 *  tool, end marks the bubble finished (it stays until the durable row replaces it). A delta
 *  with no start still opens a bubble - a reconnect may join mid-reply. A frame anchored
 *  BEFORE the newest terminal row is a late tail of a reply already on the record (the batch
 *  notifier can flush it a few ms after the row's own commit notification) and is dropped -
 *  without this rule the tail re-opened a second bubble under the finished reply. */
export function foldLive(state: ChatState, frame: ChatLive): ChatState {
  if ((frame.after_seq ?? 0) < state.settledSeq) return state;
  const open = state.live ?? { bot: "", text: "", tools: [], ended: false };
  switch (frame.type) {
    case "chat.start":
      return {
        ...state,
        live: { bot: frame.bot ?? "", text: "", tools: [], ended: false },
      };
    case "chat.delta":
      return {
        ...state,
        live: { ...open, text: open.text + frame.text, ended: false },
      };
    case "chat.activity": {
      const tools = [...open.tools];
      const id = (frame as { tool_call_id?: string }).tool_call_id ?? "";
      if (frame.phase === "start") {
        tools.push({
          id,
          tool: frame.tool ?? "",
          label: frame.label ?? "",
          done: false,
          ok: null,
        });
      } else {
        // pair by call id; a wire without ids (or an unknown id) closes the newest open tool
        let i = id ? tools.findIndex((t) => t.id === id && !t.done) : -1;
        for (let j = tools.length - 1; i < 0 && j >= 0; j--) if (!tools[j]!.done) i = j;
        if (i >= 0) tools[i] = { ...tools[i]!, done: true, ok: frame.ok ?? null };
      }
      return { ...state, live: { ...open, tools } };
    }
    case "chat.end":
      return { ...state, live: { ...open, ended: true } };
    default:
      return state;
  }
}

/** The rows that ARE a task's card in the thread: its start, its continuations,
 *  its delivery, failure or cancel, a gate it opened. A bot line that answered one of its later
 *  turns is a message of its own, not part of the card - but it does settle the card. */
export const CARD_KINDS: ReadonlySet<string> = new Set([
  "started",
  "continued",
  "delivered",
  "failed",
  "cancelled",
  "gate",
]);

/** The kinds that end a turn of the task. */
export const SETTLE_KINDS: ReadonlySet<string> = new Set(["delivered", "failed", "cancelled"]);

export type Cards = {
  /** Every card row of a task, in seq order. */
  byTask: Map<string, MessageOut[]>;
  /** Where the card sits in the thread: the seq of its first row (the task's start). */
  firstSeq: Map<string, number>;
  /** The newest BOT line that came home for a task (a continued turn's answer), by task. */
  botSeq: Map<string, number>;
};

/** Fold a task's rows into ONE card, placed where the task started. Pure. */
export function cardsOf(rows: MessageOut[]): Cards {
  const byTask = new Map<string, MessageOut[]>();
  const firstSeq = new Map<string, number>();
  const botSeq = new Map<string, number>();
  for (const m of rows) {
    if (!m.task_id) continue;
    if (m.role === "bot") {
      botSeq.set(m.task_id, Math.max(botSeq.get(m.task_id) ?? 0, m.seq));
      continue;
    }
    if (m.role !== "system" || !CARD_KINDS.has(m.kind)) continue;
    const list = byTask.get(m.task_id) ?? [];
    list.push(m);
    byTask.set(m.task_id, list);
    if (!firstSeq.has(m.task_id)) firstSeq.set(m.task_id, m.seq);
  }
  return { byTask, firstSeq, botSeq };
}

export type CardSettle = {
  /** Nothing of the task is running: its LAST turn came home. */
  settled: boolean;
  /** The row that settled the last turn (delivered | failed | cancelled), or null while it
   *  runs - and null when the last turn came home as the bot's own line. */
  row: MessageOut | null;
};

/** How a card stands: its LAST card row decides (a reply typed under a delivered card starts
 *  a new turn - `continued` - and the card is live again until THAT turn settles, as a settle
 *  row or as the bot's line that came home after it). Pure. */
export function cardSettle(cardRows: MessageOut[], botSeq: number | undefined): CardSettle {
  const last = cardRows[cardRows.length - 1];
  if (!last) return { settled: false, row: null };
  if (SETTLE_KINDS.has(last.kind)) return { settled: true, row: last };
  if (botSeq !== undefined && botSeq > last.seq) return { settled: true, row: null };
  return { settled: false, row: null };
}

/** Does a reply look like it wanted to be a file? A title plus sections, or past a length
 *  line. The heuristic only decides whether to OFFER the one tap; the person
 *  decides whether to take it. */
export function documentShaped(text: string): boolean {
  const t = text.trim();
  if (t.length > 2500) return true;
  const headings = t.split("\n").filter((l) => /^#{1,3}\s+\S/.test(l)).length;
  return /^#\s+\S/.test(t) && headings >= 2;
}

/** Is a reply owed and not yet on the record? The newest line is the owner's, the door said
 *  answer, and no bot row has landed after it. */
export function replyPending(state: ChatState): boolean {
  const last = state.rows[state.rows.length - 1];
  return Boolean(
    last && last.role === "user" && last.verdict === "answer" && last.status === "sent",
  );
}
