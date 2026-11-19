/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// Presence is state about the people on the task (ADR-062), never the thread: who has a tab
// open on this task, and who is mid-keystroke. These tests pin the two clocks (30 s present, 6 s
// typing), the person-first keying (a conn is visible to everyone who sees a frame, so it is
// never an identity), and the client's own typing sender.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { TaskEvent } from "~/lib/api/events";
import { personName } from "~/lib/format";
import {
  answeredFirst,
  createPresence,
  createTypingSender,
  emptyPresence,
  foldPresence,
  isPresenceFrame,
  mintConn,
  presenceKey,
  presentPeople,
  typingLine,
  typingMode,
  typingPeople,
  type PresenceFrame,
  type PresenceState,
} from "./presence";

const ADA = { type: "user", id: "ada-id", name: "Ada" } as const;
const BOB = { type: "user", id: "bob-id", name: "Bob" } as const;

const S = 1_000;
const T0 = 1_000_000;

const frame = (type: PresenceFrame["type"], over: Record<string, unknown> = {}): PresenceFrame =>
  ({
    type,
    task_id: "t1",
    conn: "conn-ada-1",
    actor: ADA,
    editor: true,
    at: "2026-09-30T12:00:00Z",
    ...over,
  }) as unknown as PresenceFrame;

const fold = (
  frames: [PresenceFrame, number][],
  from: PresenceState = emptyPresence(),
): PresenceState => frames.reduce((s, [f, at]) => foldPresence(s, f, at), from);

describe("who is here - the 30 s clock", () => {
  it("a join, then a here 10 s later: present at +35 s (the here refreshed it), gone at +41 s", () => {
    const s = fold([
      [frame("presence.join"), T0],
      [frame("presence.here"), T0 + 10 * S],
    ]);
    expect(presentPeople(s, T0 + 35 * S).map((p) => p.id)).toEqual(["ada-id"]);
    expect(presentPeople(s, T0 + 41 * S)).toEqual([]);
  });

  it("without a refresh the join alone lapses at 30 s - a dead tab or a crashed replica", () => {
    const s = fold([[frame("presence.join"), T0]]);
    expect(presentPeople(s, T0 + 29 * S)).toHaveLength(1);
    expect(presentPeople(s, T0 + 30 * S)).toEqual([]);
  });

  it("two conns for one actor count once, and a leave of one keeps the person here", () => {
    const s = fold([
      [frame("presence.join", { conn: "conn-ada-1" }), T0],
      [frame("presence.join", { conn: "conn-ada-2" }), T0 + S],
    ]);
    expect(presentPeople(s, T0 + 2 * S)).toEqual([{ id: "ada-id", name: "Ada", editor: true }]);
    const after = foldPresence(s, frame("presence.leave", { conn: "conn-ada-1" }), T0 + 3 * S);
    expect(presentPeople(after, T0 + 4 * S).map((p) => p.id)).toEqual(["ada-id"]);
    const gone = foldPresence(after, frame("presence.leave", { conn: "conn-ada-2" }), T0 + 5 * S);
    expect(presentPeople(gone, T0 + 6 * S)).toEqual([]);
  });

  it("editor:false shows the person as watching", () => {
    const s = fold([
      [
        frame("presence.join", {
          actor: BOB,
          conn: "conn-bob-1",
          editor: false,
        }),
        T0,
      ],
    ]);
    expect(presentPeople(s, T0 + S)).toEqual([{ id: "bob-id", name: "Bob", editor: false }]);
  });

  it("a frame with no editor flag is an editor (the field is additive on the wire)", () => {
    const s = fold([[frame("presence.join", { editor: undefined }), T0]]);
    expect(presentPeople(s, T0 + S)[0].editor).toBe(true);
  });

  it("people keep the order they arrived in, so the stack never shuffles on a refresh", () => {
    const s = fold([
      [frame("presence.join", { actor: BOB, conn: "conn-bob-1" }), T0],
      [frame("presence.join"), T0 + S],
      [frame("presence.here", { actor: BOB, conn: "conn-bob-1" }), T0 + 2 * S],
    ]);
    expect(presentPeople(s, T0 + 3 * S).map((p) => p.name)).toEqual(["Bob", "Ada"]);
  });
});

describe("identity is the person, never the conn", () => {
  it("the same conn under two actors stays two people - a hostile frame cannot merge or evict", () => {
    const s = fold([
      [frame("presence.join", { conn: "shared-conn-1" }), T0],
      [frame("presence.join", { actor: BOB, conn: "shared-conn-1" }), T0 + S],
    ]);
    expect(
      presentPeople(s, T0 + 2 * S)
        .map((p) => p.id)
        .sort(),
    ).toEqual(["ada-id", "bob-id"]);
    // Bob leaving on the shared conn takes only BOB's connection with it.
    const after = foldPresence(
      s,
      frame("presence.leave", { actor: BOB, conn: "shared-conn-1" }),
      T0 + 3 * S,
    );
    expect(presentPeople(after, T0 + 4 * S).map((p) => p.id)).toEqual(["ada-id"]);
  });

  it("selfConn floats its holder to the front - but only when exactly one person holds it", () => {
    const s = fold([
      [frame("presence.join", { actor: BOB, conn: "conn-bob-1" }), T0],
      [frame("presence.join", { conn: "conn-ada-1" }), T0 + S],
    ]);
    expect(presentPeople(s, T0 + 2 * S, "conn-ada-1").map((p) => p.name)).toEqual(["Ada", "Bob"]);
    // A hostile editor replays MY conn under their own name: ambiguous, so nobody moves.
    const spoofed = foldPresence(
      s,
      frame("presence.join", { actor: BOB, conn: "conn-ada-1" }),
      T0 + 3 * S,
    );
    expect(presentPeople(spoofed, T0 + 4 * S, "conn-ada-1").map((p) => p.name)).toEqual([
      "Bob",
      "Ada",
    ]);
  });
});

describe("who is typing - the 6 s clock", () => {
  const on = (over: Record<string, unknown> = {}) =>
    frame("presence.typing", { on: true, mode: "follow_up", ...over });

  it("typing{on} at t: typing at +5 s, not at +7 s", () => {
    const s = fold([[on(), T0]]);
    expect(typingPeople(s, T0 + 5 * S)).toEqual([{ id: "ada-id", name: "Ada", mode: "follow_up" }]);
    expect(typingPeople(s, T0 + 7 * S)).toEqual([]);
  });

  it("a fresh typing{on} keeps it alive - the client re-sends while keys land", () => {
    const s = fold([
      [on(), T0],
      [on(), T0 + 4 * S],
    ]);
    expect(typingPeople(s, T0 + 9 * S)).toHaveLength(1);
    expect(typingPeople(s, T0 + 11 * S)).toEqual([]);
  });

  it("typing{on:false} clears the flag at once", () => {
    const s = fold([
      [on(), T0],
      [on({ on: false }), T0 + S],
    ]);
    expect(typingPeople(s, T0 + 2 * S)).toEqual([]);
  });

  it("the viewer's own person is excluded, by actor - another tab of mine is still me", () => {
    const s = fold([
      [on({ conn: "conn-ada-2" }), T0],
      [on({ actor: BOB, conn: "conn-bob-1", mode: "steer" }), T0],
    ]);
    expect(typingPeople(s, T0 + S, "ada-id")).toEqual([
      { id: "bob-id", name: "Bob", mode: "steer" },
    ]);
  });

  it("carries the mode: steer and answer read differently from a plain follow-up", () => {
    const s = fold([
      [on({ mode: "answer" }), T0],
      [on({ actor: BOB, conn: "conn-bob-1", mode: "steer" }), T0],
    ]);
    expect(typingPeople(s, T0 + S).map((p) => p.mode)).toEqual(["answer", "steer"]);
  });

  it("a leave takes the typing flag with it", () => {
    const s = fold([
      [on(), T0],
      [frame("presence.leave"), T0 + S],
    ]);
    expect(typingPeople(s, T0 + 2 * S)).toEqual([]);
  });

  it("a late typing:false for a person who already left does not resurrect them", () => {
    const s = fold([
      [frame("presence.join"), T0],
      [frame("presence.leave"), T0 + S],
      [on({ on: false }), T0 + 2 * S],
    ]);
    expect(presentPeople(s, T0 + 3 * S)).toEqual([]);
  });

  it("typing from a person we never saw join still puts them in the stack", () => {
    const s = fold([[on(), T0]]);
    expect(presentPeople(s, T0 + S).map((p) => p.id)).toEqual(["ada-id"]);
  });
});

describe("state stays small and honest", () => {
  it("folding prunes what has expired - a long-lived page never accretes ghosts", () => {
    let s = fold([[frame("presence.join", { conn: "conn-old-1" }), T0]]);
    s = foldPresence(s, frame("presence.join", { actor: BOB, conn: "conn-bob-1" }), T0 + 60 * S);
    expect(s.people.map((p) => p.id)).toEqual(["bob-id"]);
  });

  it("presenceKey moves exactly when what is drawn moves - the hook re-renders on it, not per frame", () => {
    const s = fold([[frame("presence.join"), T0]]);
    const k = presenceKey(s, T0 + S);
    // a `here` refresh changes nothing visible
    const refreshed = foldPresence(s, frame("presence.here"), T0 + 10 * S);
    expect(presenceKey(refreshed, T0 + 11 * S)).toBe(k);
    // the person lapsing does
    expect(presenceKey(s, T0 + 31 * S)).not.toBe(k);
    // and so does someone starting to type
    const typing = foldPresence(
      s,
      frame("presence.typing", { on: true, mode: "steer" }),
      T0 + 2 * S,
    );
    expect(presenceKey(typing, T0 + 3 * S)).not.toBe(k);
  });

  it("isPresenceFrame names the four presence types and nothing else", () => {
    const of = (type: string) => ({ type }) as unknown as TaskEvent;
    for (const t of ["join", "leave", "here", "typing"])
      expect(isPresenceFrame(of(`presence.${t}`))).toBe(true);
    for (const t of ["task.status", "progress", "message.delta"])
      expect(isPresenceFrame(of(t))).toBe(false);
  });
});

describe("typingLine - one muted sentence above the composer", () => {
  it("one person names what they are doing", () => {
    expect(typingLine([{ id: "a", name: "Ada", mode: "follow_up" }])).toBe("Ada is typing…");
    expect(typingLine([{ id: "b", name: "Bob", mode: "steer" }])).toBe("Bob is steering…");
    expect(typingLine([{ id: "c", name: "Cy", mode: "answer" }])).toBe("Cy is answering…");
  });

  it("two people: 'Ada and Bob are typing…'", () => {
    expect(
      typingLine([
        { id: "a", name: "Ada", mode: "follow_up" },
        { id: "b", name: "Bob", mode: "follow_up" },
      ]),
    ).toBe("Ada and Bob are typing…");
  });

  it("a crowd collapses to a count, and a shared verb is kept when they all share it", () => {
    expect(
      typingLine([
        { id: "a", name: "Ada", mode: "steer" },
        { id: "b", name: "Bob", mode: "steer" },
        { id: "c", name: "Cy", mode: "steer" },
        { id: "d", name: "Di", mode: "steer" },
      ]),
    ).toBe("Ada, Bob and 2 others are steering…");
    expect(
      typingLine([
        { id: "a", name: "Ada", mode: "steer" },
        { id: "b", name: "Bob", mode: "answer" },
      ]),
    ).toBe("Ada and Bob are typing…");
  });

  it("nobody typing is no line at all", () => {
    expect(typingLine([])).toBeNull();
  });
});

describe("typingMode - mirrors the composer's own branches", () => {
  const asks = [{ id: "q1", prompt: "Which?", type: "text", options: [], required: true }] as never;

  it("a question gate takes the words as an answer", () => {
    expect(typingMode({ live: true, gate: { kind: "input", questions: asks } })).toBe("answer");
    expect(typingMode({ live: true, gate: { questions: asks } })).toBe("answer");
  });

  it("an approve or connect gate takes no text - the words steer, like submit()", () => {
    expect(typingMode({ live: true, gate: { kind: "approve", questions: [] } })).toBe("steer");
    expect(typingMode({ live: true, gate: { kind: "connect", questions: [] } })).toBe("steer");
  });

  it("a budget park has no questions and mounts no dock: the words steer, never 'answering'", () => {
    expect(typingMode({ live: true, gate: { kind: "budget", questions: [] } })).toBe("steer");
  });

  it("live is a steer; settled is a follow-up", () => {
    expect(typingMode({ live: true, gate: null })).toBe("steer");
    expect(typingMode({ live: false, gate: null })).toBe("follow_up");
  });
});

describe("createTypingSender - best-effort, never chatty", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(T0);
  });
  afterEach(() => vi.useRealTimers());

  it("posts at most once per 1.5 s while keys land", () => {
    const post = vi.fn();
    const sender = createTypingSender(post);
    sender.change("h");
    vi.advanceTimersByTime(500);
    sender.change("he");
    vi.advanceTimersByTime(500);
    sender.change("hel");
    expect(post).toHaveBeenCalledTimes(1);
    expect(post).toHaveBeenLastCalledWith(true);
    vi.advanceTimersByTime(600); // 1.6 s since the first post
    sender.change("hell");
    expect(post).toHaveBeenCalledTimes(2);
    expect(post).toHaveBeenLastCalledWith(true);
  });

  it("sends the final typing:false 2 s after the last keystroke", () => {
    const post = vi.fn();
    const sender = createTypingSender(post);
    sender.change("h");
    vi.advanceTimersByTime(1_900);
    expect(post).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(200);
    expect(post).toHaveBeenCalledTimes(2);
    expect(post).toHaveBeenLastCalledWith(false);
    vi.advanceTimersByTime(10_000);
    expect(post).toHaveBeenCalledTimes(2); // and then silence
  });

  it("each keystroke pushes the idle stop back", () => {
    const post = vi.fn();
    const sender = createTypingSender(post);
    sender.change("h");
    vi.advanceTimersByTime(1_500);
    sender.change("he");
    vi.advanceTimersByTime(1_500);
    expect(post).not.toHaveBeenCalledWith(false);
  });

  it("emptying the box (or sending, which clears it) stops at once - and only if it had started", () => {
    const post = vi.fn();
    const sender = createTypingSender(post);
    sender.change("");
    expect(post).not.toHaveBeenCalled(); // never typed, nothing to retract
    sender.change("hi");
    sender.change("");
    expect(post.mock.calls).toEqual([[true], [false]]);
    vi.advanceTimersByTime(5_000);
    expect(post).toHaveBeenCalledTimes(2); // the idle timer died with it
  });

  it("whitespace alone is not typing", () => {
    const post = vi.fn();
    createTypingSender(post).change("   \n");
    expect(post).not.toHaveBeenCalled();
  });

  it("typing again after a stop announces immediately, not after the throttle", () => {
    const post = vi.fn();
    const sender = createTypingSender(post);
    sender.change("a");
    sender.change("");
    sender.change("b");
    expect(post.mock.calls).toEqual([[true], [false], [true]]);
  });

  it("stop() is safe to call any time (unmount)", () => {
    const post = vi.fn();
    const sender = createTypingSender(post);
    sender.stop();
    expect(post).not.toHaveBeenCalled();
    sender.change("a");
    sender.stop();
    expect(post).toHaveBeenLastCalledWith(false);
  });
});

describe("answeredFirst - the second person to answer is told whose answer stood", () => {
  it("someone else's resolution reads 'Bob answered first'", () => {
    expect(answeredFirst("Bob", "Ada")).toBe("Bob answered first");
  });

  it("your own resolution says nothing", () => {
    expect(answeredFirst("Ada", "Ada")).toBeNull();
    expect(answeredFirst("  Ada  ", "Ada")).toBeNull();
  });

  it("says what the first person did: a skip, or a cancel that stopped the task", () => {
    const ids = { resolvedById: "u-bob", myId: "u-ada" };
    expect(answeredFirst("Bob", "Ada", { ...ids, action: "accept" })).toBe("Bob answered first");
    expect(answeredFirst("Bob", "Ada", { ...ids, action: "decline" })).toBe(
      "Bob skipped the questions first",
    );
    expect(answeredFirst("Bob", "Ada", { ...ids, action: "cancel" })).toBe(
      "Bob cancelled the task",
    );
    // A system cancel (a routine's REPLACE) names no person, so nothing is said.
    expect(
      answeredFirst("system (cancel)", "Ada", {
        resolvedById: null,
        myId: "u-ada",
        action: "cancel",
      }),
    ).toBeNull();
  });

  it("no name on either side is nothing honest to say", () => {
    expect(answeredFirst("", "Ada")).toBeNull();
    expect(answeredFirst(undefined, "Ada")).toBeNull();
    expect(answeredFirst("Bob", undefined)).toBeNull(); // who am I? unknown - stay quiet, never a false 'first'
  });
});

describe("names are data the person controls", () => {
  it("personName collapses whitespace, caps at 60, and never returns nothing", () => {
    expect(personName("  Ada\n\n  Lovelace ")).toBe("Ada Lovelace");
    expect(personName("x".repeat(100))).toHaveLength(60);
    expect(personName("x".repeat(100)).endsWith("…")).toBe(true);
    expect(personName("")).toBe("Someone");
    expect(personName(null)).toBe("Someone");
  });

  it("a presence frame's name goes through it before it is ever drawn", () => {
    const s = fold([[frame("presence.join", { actor: { ...ADA, name: "Ada\nLovelace" } }), T0]]);
    expect(presentPeople(s, T0 + S)[0].name).toBe("Ada Lovelace");
  });
});

describe("answeredFirst - by id when the server names one (two people can share a name)", () => {
  it("another Ada's answer is still someone else's", () => {
    expect(answeredFirst("Ada", "Ada", { resolvedById: "u-ada-2", myId: "u-ada-1" })).toBe(
      "Ada answered first",
    );
  });

  it("your own answer says nothing, whatever the name reads", () => {
    expect(answeredFirst("Ada L.", "Ada", { resolvedById: "u-ada", myId: "u-ada" })).toBeNull();
  });

  it("with no id on either side, the names decide, as before", () => {
    expect(answeredFirst("Bob", "Ada", { resolvedById: null, myId: "u-ada" })).toBe(
      "Bob answered first",
    );
    expect(answeredFirst("Ada", "Ada", { resolvedById: "u-x", myId: undefined })).toBeNull();
  });
});

describe("answeredFirst - never blames the clock", () => {
  it("a system resolution (a gate timeout) is not a person who answered first", () => {
    expect(answeredFirst("system (timeout)", "Ada")).toBeNull();
    expect(answeredFirst("System (anything)", "Ada")).toBeNull();
  });
});

// The presence the page reads must always be the LATEST state. The stream hook once published
// only when `presenceKey` moved, so a `here` refresh (or a repeated typing{on}) updated a
// closure but not the state the route computed from: 30 s (typing: 6 s) after the last
// visible change, any re-render drew an empty stack. These pin the publisher that replaced it.
describe("createPresence - the page always reads the latest state", () => {
  const setup = () => {
    const published: PresenceState[] = [];
    const publisher = createPresence(
      (r) => published.push(r),
      () => T0,
    );
    const last = () => published[published.length - 1];
    // what the route does on ANY re-render: compute from the latest published state at `now`
    const drawn = (now: number) => presentPeople(last() ?? emptyPresence(), now).map((p) => p.id);
    const typing = (now: number) => typingPeople(last() ?? emptyPresence(), now).map((p) => p.id);
    const hear = (f: PresenceFrame, at: number) =>
      publisher.frame(foldPresence(publisher.get(), f, at), at);
    return { publisher, published, drawn, typing, hear };
  };

  it("a person whose here arrives at 25 s is still drawn at 40 s", () => {
    const { drawn, hear } = setup();
    hear(frame("presence.join"), T0);
    hear(frame("presence.here"), T0 + 10 * S);
    hear(frame("presence.here"), T0 + 25 * S); // a refresh: nothing visible changed
    expect(drawn(T0 + 40 * S)).toEqual(["ada-id"]); // 15 s after the last here: still here
    expect(drawn(T0 + 56 * S)).toEqual([]); // and gone once THAT lapses (31 s)
  });

  it("someone re-sending typing{on} every 1.5 s is still typing at 10.5 s", () => {
    const { typing, hear } = setup();
    for (let t = 0; t <= 10_500; t += 1_500)
      hear(frame("presence.typing", { on: true, mode: "steer" }), T0 + t);
    expect(typing(T0 + 10_500)).toEqual(["ada-id"]);
    expect(typing(T0 + 10_500 + 6 * S)).toEqual([]);
  });

  it("every presence frame publishes a fresh state object (React must see it)", () => {
    const { published, hear } = setup();
    hear(frame("presence.join"), T0);
    hear(frame("presence.here"), T0 + 10 * S);
    expect(published).toHaveLength(2);
    expect(published[0]).not.toBe(published[1]);
  });

  it("the 1 s clock publishes only when what is drawn moved - a lapse - never on an idle tick", () => {
    const { publisher, published, hear } = setup();
    hear(frame("presence.join"), T0);
    const after = published.length;
    for (let t = 1; t <= 29; t++) publisher.tick(T0 + t * S);
    expect(published).toHaveLength(after); // nothing moved: no render
    publisher.tick(T0 + 30 * S); // the person lapses
    expect(published).toHaveLength(after + 1);
    publisher.tick(T0 + 31 * S);
    expect(published).toHaveLength(after + 1); // and it does not repeat
  });

  it("typing that lapses on the clock publishes once", () => {
    const { publisher, published, hear } = setup();
    hear(frame("presence.typing", { on: true, mode: "follow_up" }), T0);
    const after = published.length;
    publisher.tick(T0 + 5 * S);
    expect(published).toHaveLength(after);
    publisher.tick(T0 + 6 * S);
    expect(published).toHaveLength(after + 1);
  });

  it("reset empties the stack and says so", () => {
    const { publisher, published, hear, drawn } = setup();
    hear(frame("presence.join"), T0);
    publisher.reset();
    expect(publisher.get().people).toEqual([]);
    expect(drawn(T0 + S)).toEqual([]);
    expect(published.length).toBeGreaterThan(1);
  });
});

describe("mintConn - one id per tab, even where crypto.randomUUID does not exist", () => {
  afterEach(() => vi.unstubAllGlobals());
  const VALID = /^[A-Za-z0-9_-]{8,64}$/; // the server's own rule

  it("is a valid conn and differs per call", () => {
    const a = mintConn();
    expect(a).toMatch(VALID);
    expect(mintConn()).not.toBe(a);
  });

  it("falls back to getRandomValues on a non-secure origin (no randomUUID there)", () => {
    vi.stubGlobal("crypto", {
      getRandomValues: (b: Uint8Array) => {
        for (let i = 0; i < b.length; i++) b[i] = (i * 37 + 11) % 256;
        return b;
      },
    });
    expect(mintConn()).toMatch(VALID);
  });

  it("and to Math.random when there is no crypto at all", () => {
    vi.stubGlobal("crypto", undefined);
    expect(mintConn()).toMatch(VALID);
  });
});
