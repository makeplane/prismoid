/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { describe, expect, it } from "vitest";
import { badgeStale, cardSeenKey, seenKey, shouldMarkSeen } from "./seen-task";

const gate = (gate_id: string) => ({ gate_id });

describe("seenKey", () => {
  it("moves when the turn settles", () => {
    expect(seenKey({ turn: 1, status: "running", gates: [] })).not.toBe(
      seenKey({ turn: 1, status: "delivered", gates: [] }),
    );
  });

  it("moves when a gate opens or closes", () => {
    const before = seenKey({ turn: 1, status: "waiting", gates: [] });
    const asked = seenKey({ turn: 1, status: "waiting", gates: [gate("g1")] });
    expect(asked).not.toBe(before);
    expect(seenKey({ turn: 1, status: "waiting", gates: [gate("g1"), gate("g2")] })).not.toBe(
      asked,
    );
  });

  it("moves when a turn settles and the next starts while the tab is hidden", () => {
    // Shown at turn 1 running; hidden through its delivery and a teammate's follow-up.
    expect(seenKey({ turn: 2, status: "running", gates: [] })).not.toBe(
      seenKey({ turn: 1, status: "running", gates: [] }),
    );
  });

  it("does not move for the gates' order", () => {
    expect(seenKey({ turn: 1, status: "waiting", gates: [gate("a"), gate("b")] })).toBe(
      seenKey({ turn: 1, status: "waiting", gates: [gate("b"), gate("a")] }),
    );
  });
});

describe("cardSeenKey", () => {
  const rows = (...seqs: number[]) => seqs.map((seq) => ({ seq }));

  it("moves when the card gets a row: a gate asked, a turn settled", () => {
    expect(cardSeenKey(rows(1, 2), undefined)).not.toBe(cardSeenKey(rows(1), undefined));
    expect(cardSeenKey(rows(1, 2, 3), undefined)).not.toBe(cardSeenKey(rows(1, 2), undefined));
  });

  it("moves when a continued turn comes home as the bot's line", () => {
    expect(cardSeenKey(rows(1, 3), 7)).not.toBe(cardSeenKey(rows(1, 3), undefined));
  });

  it("does not move when nothing new came", () => {
    expect(cardSeenKey(rows(1, 3), 2)).toBe(cardSeenKey(rows(1, 3), undefined));
  });
});

describe("shouldMarkSeen", () => {
  it("reports a page that opens shown", () => {
    expect(shouldMarkSeen("1|running|", null, true)).toBe(true);
  });

  it("does not report the same news twice", () => {
    expect(shouldMarkSeen("1|running|", "1|running|", true)).toBe(false);
  });

  it("reports news the stream brings", () => {
    expect(shouldMarkSeen("1|delivered|", "1|running|", true)).toBe(true);
  });

  it("waits while the tab is hidden, and reports once it is shown", () => {
    expect(shouldMarkSeen("1|delivered|", "1|running|", false)).toBe(false);
    expect(shouldMarkSeen("1|delivered|", "1|running|", true)).toBe(true);
  });
});

describe("badgeStale", () => {
  it("re-reads when the call read something", () => {
    expect(badgeStale({ marked: 1, unread: 2 }, 3)).toBe(true);
  });

  it("re-reads when the server read the knocks itself (a gate answered here)", () => {
    expect(badgeStale({ marked: 0, unread: 2 }, 3)).toBe(true);
  });

  it("leaves a badge that already matches", () => {
    expect(badgeStale({ marked: 0, unread: 3 }, 3)).toBe(false);
  });

  it("outside the shell there is no badge to fix", () => {
    expect(badgeStale({ marked: 0, unread: 3 }, undefined)).toBe(false);
  });
});
