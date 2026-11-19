/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { describe, expect, it } from "vitest";
import { createFrameLog, resumeSeq, type KeptFrame } from "./frame-log";

const f = (seq: number): KeptFrame => ({ type: "step.state", data: `{"seq":${seq}}`, seq });

describe("the task frame log", () => {
  it("a first mount starts from 0, and a return resumes after the newest kept seq", () => {
    const log = createFrameLog();
    expect(resumeSeq(log.open("t1", "tok"))).toBe(0);
    log.keep("t1", "tok", f(1));
    log.keep("t1", "tok", f(4));
    const kept = log.open("t1", "tok");
    expect(kept.map((k) => k.seq)).toEqual([1, 4]);
    expect(resumeSeq(kept)).toBe(4);
  });

  it("a replayed or older frame is never kept twice", () => {
    const log = createFrameLog();
    log.open("t1", "tok");
    log.keep("t1", "tok", f(2));
    log.keep("t1", "tok", f(2));
    log.keep("t1", "tok", f(1));
    expect(log.open("t1", "tok").map((k) => k.seq)).toEqual([2]);
  });

  it("keeps nothing for a task no mount opened, so a log is always complete from its start", () => {
    const log = createFrameLog();
    log.keep("t1", "tok", f(7));
    expect(log.open("t1", "tok")).toEqual([]);
  });

  it("another credential never reads a log, and replaces it", () => {
    const log = createFrameLog();
    log.open("t1", "ada");
    log.keep("t1", "ada", f(1));
    expect(log.open("t1", "bob")).toEqual([]);
    log.keep("t1", "ada", f(2)); // Ada's stale mount can no longer write into Bob's log
    expect(log.open("t1", "bob")).toEqual([]);
  });

  it("evicts the least recently opened task past its size, whole", () => {
    const log = createFrameLog({ maxTasks: 2 });
    for (const id of ["a", "b"]) {
      log.open(id, "tok");
      log.keep(id, "tok", f(1));
    }
    log.open("a", "tok"); // a is now the most recent
    log.open("c", "tok"); // b goes
    log.keep("b", "tok", f(2)); // a mount of b still alive keeps nothing
    expect(log.open("a", "tok")).toHaveLength(1);
    expect(log.open("b", "tok")).toEqual([]);
  });

  it("drops a log that grows past its cap, so the next mount replays from 0", () => {
    const log = createFrameLog({ maxFrames: 2 });
    log.open("t1", "tok");
    log.keep("t1", "tok", f(1));
    log.keep("t1", "tok", f(2));
    log.keep("t1", "tok", f(3));
    log.keep("t1", "tok", f(4));
    expect(log.open("t1", "tok")).toEqual([]);
  });

  it("hands out a copy: a caller cannot edit what is kept", () => {
    const log = createFrameLog();
    log.open("t1", "tok");
    log.keep("t1", "tok", f(1));
    const copy = log.open("t1", "tok") as KeptFrame[];
    copy.push(f(9));
    expect(resumeSeq(log.open("t1", "tok"))).toBe(1);
  });
});
