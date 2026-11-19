/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// Presence, pure (ADR-062): who has this task open, and who is mid-keystroke. State about the
// people on the task, never the thread - the reducer (lib/task-view.ts) never sees a presence
// frame, and the stream hook (hooks/use-task-stream.ts) folds them here instead.
//
// Two clocks, both on the CLIENT (a dead tab or a crashed API replica never says leave):
//   · a person is HERE while any of their connections said join/here within 30 s;
//   · a person is TYPING for 6 s after their last `typing{on}`.
//
// Keyed by PERSON first. A `conn` is visible to everyone who sees a frame, and the server
// only shapes it - it does not bind it to a person - so a conn is never trusted as identity:
// each person carries their own set of conns, and the same conn under two actors is two
// people. Time is the caller's `now`, stamped at receipt, never the frame's `at` (a skewed
// clock must not make someone immortal).

import type { TaskEvent } from "~/lib/api/events";
import { personName } from "~/lib/format";
import { dockFor, type GateView } from "~/lib/task-view";

/** Present while any connection was heard within this window (join/here every ~10 s). */
export const PRESENCE_TTL_MS = 30_000;
/** Typing while the last `typing{on}` is younger than this (the client re-sends every 1.5 s). */
export const TYPING_TTL_MS = 6_000;

export type TypingMode = "follow_up" | "steer" | "answer";

export type PresenceFrame = Extract<
  TaskEvent,
  {
    type: "presence.join" | "presence.leave" | "presence.here" | "presence.typing";
  }
>;

export function isPresenceFrame(ev: TaskEvent): ev is PresenceFrame {
  return ev.type.startsWith("presence.");
}

type Person = {
  id: string;
  name: string;
  editor: boolean;
  /** conn -> when a join/here/typing-on last vouched for it (client ms). */
  conns: ReadonlyMap<string, number>;
  /** conn -> the newest `typing{on}` from that tab. */
  typing: ReadonlyMap<string, { mode: TypingMode; at: number }>;
};

/** People in the order they first arrived - the header stack never shuffles on a refresh. */
export type PresenceState = { readonly people: readonly Person[] };

export function emptyPresence(): PresenceState {
  return { people: [] };
}

/** Drop what has expired. Returns the SAME state when nothing did, so callers can compare. */
function prune(state: PresenceState, now: number): PresenceState {
  let changed = false;
  const people: Person[] = [];
  for (const p of state.people) {
    const conns = new Map<string, number>();
    for (const [conn, at] of p.conns) if (now - at < PRESENCE_TTL_MS) conns.set(conn, at);
    if (conns.size === 0) {
      changed = true;
      continue;
    }
    const typing = new Map<string, { mode: TypingMode; at: number }>();
    for (const [conn, t] of p.typing)
      if (conns.has(conn) && now - t.at < TYPING_TTL_MS) typing.set(conn, t);
    if (conns.size === p.conns.size && typing.size === p.typing.size) {
      people.push(p);
    } else {
      changed = true;
      people.push({ ...p, conns, typing });
    }
  }
  return changed ? { people } : state;
}

/** Fold one presence frame, received at `now` (ms). Pure: a new state, or the same one. */
export function foldPresence(
  state: PresenceState,
  frame: PresenceFrame,
  now: number,
): PresenceState {
  const live = prune(state, now);
  const id = frame.actor.id;
  const i = live.people.findIndex((p) => p.id === id);
  const cur = i === -1 ? undefined : live.people[i];
  const conn = frame.conn;

  const put = (person: Person | null): PresenceState => {
    const people = live.people.slice();
    if (i === -1) {
      if (person) people.push(person);
    } else if (person) {
      people[i] = person;
    } else {
      people.splice(i, 1);
    }
    return { people };
  };

  if (frame.type === "presence.leave") {
    if (!cur) return live;
    const conns = new Map(cur.conns);
    conns.delete(conn);
    if (conns.size === 0) return put(null);
    const typing = new Map(cur.typing);
    typing.delete(conn);
    return put({ ...cur, conns, typing });
  }

  if (frame.type === "presence.typing" && !frame.on) {
    // Only ever a retraction: a late `off` for someone who already left must not put them
    // back in the stack.
    if (!cur || !cur.typing.has(conn)) return live;
    const typing = new Map(cur.typing);
    typing.delete(conn);
    return put({ ...cur, typing });
  }

  // join · here · typing{on}: each one vouches that this connection is alive right now.
  const conns = new Map(cur?.conns ?? []);
  conns.set(conn, now);
  const typing = new Map(cur?.typing ?? []);
  if (frame.type === "presence.typing") typing.set(conn, { mode: frame.mode, at: now });
  return put({
    id,
    name: personName(frame.actor.name),
    editor: frame.editor !== false,
    conns,
    typing,
  });
}

export type PresentPerson = { id: string; name: string; editor: boolean };

/**
 * Who is here at `now`, one entry per PERSON however many tabs they have open.
 *
 * `selfConn` (this tab's own connection) floats its holder to the front - and only when
 * exactly one person holds it. A conn is not an identity (a hostile editor can replay yours
 * under their own name), so an ambiguous claim moves nobody.
 */
export function presentPeople(
  state: PresenceState,
  now: number,
  selfConn?: string,
): PresentPerson[] {
  const here = state.people.filter((p) =>
    [...p.conns.values()].some((at) => now - at < PRESENCE_TTL_MS),
  );
  const holders = selfConn ? here.filter((p) => p.conns.has(selfConn)) : [];
  const ordered =
    holders.length === 1 ? [holders[0], ...here.filter((p) => p !== holders[0])] : here;
  return ordered.map(({ id, name, editor }) => ({ id, name, editor }));
}

export type TypingPerson = { id: string; name: string; mode: TypingMode };

/** Who is typing at `now`, excluding the viewer's own PERSON (another tab of mine is still me). */
export function typingPeople(
  state: PresenceState,
  now: number,
  selfActorId?: string,
): TypingPerson[] {
  const out: TypingPerson[] = [];
  for (const p of state.people) {
    if (p.id === selfActorId) continue;
    let newest: { mode: TypingMode; at: number } | null = null;
    for (const t of p.typing.values())
      if (now - t.at < TYPING_TTL_MS && (!newest || t.at > newest.at)) newest = t;
    if (newest) out.push({ id: p.id, name: p.name, mode: newest.mode });
  }
  return out;
}

/** What is DRAWN at `now`, as a string. The 1 s clock (`createPresence`'s `tick`) publishes
 *  only when this moves - exactly when a person lapses or a typing line runs out - so an idle
 *  second re-renders nothing. A presence FRAME always publishes (the state must stay fresh). */
export function presenceKey(state: PresenceState, now: number): string {
  const here = presentPeople(state, now)
    .map((p) => `${p.id}:${p.name}:${p.editor ? "e" : "w"}`)
    .join(",");
  const typing = typingPeople(state, now)
    .map((p) => `${p.id}:${p.mode}`)
    .join(",");
  return `${here}|${typing}`;
}

const VERB: Record<TypingMode, string> = {
  follow_up: "typing",
  steer: "steering",
  answer: "answering",
};

/** The one muted line above the composer: "Ada is typing…", "Bob is steering…",
 *  "Ada and Bob are typing…". Null when nobody is. */
export function typingLine(people: readonly TypingPerson[]): string | null {
  if (people.length === 0) return null;
  const modes = new Set(people.map((p) => p.mode));
  // A shared verb is kept ("are steering"); a mix collapses to the plain one.
  const verb = modes.size === 1 ? VERB[people[0].mode] : "typing";
  if (people.length === 1) return `${people[0].name} is ${verb}…`;
  const names = people.map((p) => p.name);
  const subject =
    names.length === 2
      ? `${names[0]} and ${names[1]}`
      : names.length === 3
        ? `${names[0]}, ${names[1]} and ${names[2]}`
        : `${names[0]}, ${names[1]} and ${names.length - 2} others`;
  return `${subject} are ${verb}…`;
}

/** The typing frame's mode, chosen from the composer's OWN branches (route submit(), both
 *  through `dockFor`): a question dock takes the words as an answer; an approve or connect gate
 *  takes no text, and neither does a budget park (it mounts BudgetDock, no words) or a bare
 *  questionless gate (no dock at all), so they steer; a live task steers; a settled one continues with a follow-up. */
export function typingMode(t: {
  live: boolean;
  gate?: Pick<GateView, "kind" | "questions"> | null;
}): TypingMode {
  if (t.gate && dockFor(t.gate) === "questions") return "answer";
  return t.live ? "steer" : "follow_up";
}

export const TYPING_THROTTLE_MS = 1_500;
export const TYPING_IDLE_MS = 2_000;

export type TypingSender = {
  /** The composer's text changed. */
  change: (text: string) => void;
  /** Retract now (a send, an unmount). Safe any time. */
  stop: () => void;
};

/**
 * The client's half of typing: at most one `typing{on}` per 1.5 s while keys land, and a final
 * `typing{off}` 2 s after the last one (or the instant the box empties, which is also what a
 * send does). `post` is best-effort by contract: the caller swallows its errors.
 */
export function createTypingSender(
  post: (typing: boolean) => void,
  opts: { throttleMs?: number; idleMs?: number } = {},
): TypingSender {
  const throttleMs = opts.throttleMs ?? TYPING_THROTTLE_MS;
  const idleMs = opts.idleMs ?? TYPING_IDLE_MS;
  let active = false;
  let lastOn = Number.NEGATIVE_INFINITY;
  let idle: ReturnType<typeof setTimeout> | null = null;

  const clearIdle = () => {
    if (idle != null) clearTimeout(idle);
    idle = null;
  };
  const stop = () => {
    clearIdle();
    if (!active) return;
    active = false;
    lastOn = Number.NEGATIVE_INFINITY;
    post(false);
  };
  const change = (text: string) => {
    if (text.trim() === "") {
      stop();
      return;
    }
    const now = Date.now();
    if (!active || now - lastOn >= throttleMs) {
      active = true;
      lastOn = now;
      post(true);
    }
    clearIdle();
    idle = setTimeout(stop, idleMs);
  };
  return { change, stop };
}

/** "Bob answered first" - when the server hands back a resolution that is not yours. Two people
 *  can be looking at one question; the second to submit is given the first one's resolution
 *  rather than an error (operations/gates.py `resolve`), and should be told whose it was - and
 *  what it was: an answer, a skip, or a cancel that stopped the task. Null when it was yours,
 *  when the server named no one, or when the resolver was the system (nothing honest to say).
 *  "Yours" is decided by id when both ids are known (the resolution's `resolved_by_id` and your
 *  own) - two people can share a name - and by name otherwise. */
export function answeredFirst(
  resolvedBy: string | null | undefined,
  myName: string | null | undefined,
  ids: {
    resolvedById?: string | null;
    myId?: string | null;
    action?: string;
  } = {},
): string | null {
  if (!resolvedBy?.trim()) return null;
  // The clock and a routine resolve too ("system (timeout)", "system (cancel)",
  // operations/gates.py): not a person, so never "system (timeout) answered first".
  if (/^system \(/i.test(resolvedBy.trim())) return null;
  const who = personName(resolvedBy);
  const mine =
    ids.resolvedById && ids.myId
      ? ids.resolvedById === ids.myId
      : !myName?.trim() || who === personName(myName);
  if (mine) return null;
  if (ids.action === "cancel") return `${who} cancelled the task`;
  if (ids.action === "decline") return `${who} skipped the questions first`;
  return `${who} answered first`;
}

/** The publisher between presence and React. The page reads `presentPeople(state, now)` on ANY
 *  re-render, so the state it holds must always be the LATEST state, not the state as it stood
 *  when what is drawn last changed (the bug this replaced: publishing only when `presenceKey`
 *  moved left the state stale, and 30 s after the last visible change - 6 s for typing - any
 *  re-render drew an empty stack).
 *
 *  - `frame(next, at)`: a presence frame folded. ALWAYS publishes a fresh state object (one
 *    render per frame: about one per 10 s per person, and one per 1.5 s per typist).
 *  - `tick(at)`: the 1 s clock. The state did not change, only the time did, so it publishes
 *    only when what is drawn moved (a person or a typing line lapsed) - never on an idle tick.
 *  - `get()` is the latest state, for folding the next frame onto. */
export function createPresence(
  publish: (state: PresenceState) => void,
  now: () => number = Date.now,
) {
  let current = emptyPresence();
  let drawn = presenceKey(current, now());
  // A fresh top-level object each time: React bails out on an identical reference, and a
  // lapse changes nothing in `people` itself.
  const out = () => publish({ people: current.people });
  return {
    get: (): PresenceState => current,
    frame(next: PresenceState, at: number): void {
      current = next;
      drawn = presenceKey(current, at);
      out();
    },
    tick(at: number): void {
      const key = presenceKey(current, at);
      if (key === drawn) return;
      drawn = key;
      out();
    },
    reset(): void {
      current = emptyPresence();
      drawn = presenceKey(current, now());
      out();
    },
  };
}

/** One connection id per open tab per task, `[A-Za-z0-9_-]{8,64}` (the server's rule). Never an
 *  identity (see the top of this file). `crypto.randomUUID` exists only in secure contexts, and
 *  this runs at mount - on a plain-http LAN address it would take the whole page down - so it
 *  falls back to `getRandomValues` (available everywhere), then to `Math.random`. */
export function mintConn(): string {
  const c: Crypto | undefined = globalThis.crypto;
  let hex: string;
  if (typeof c?.randomUUID === "function") {
    hex = c.randomUUID().replaceAll("-", "");
  } else {
    const bytes = new Uint8Array(16);
    if (typeof c?.getRandomValues === "function") c.getRandomValues(bytes);
    else for (let i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256);
    hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  }
  return "t" + hex.slice(0, 20);
}
