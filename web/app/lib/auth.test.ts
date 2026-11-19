/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The round trip a join link makes: /join/<code> logged out -> /login?next=<the link> -> back.
// The login page redirects to `safeNext(next)`, so this pair decides whether a link survives sign-in
// and whether a crafted `next` can take a person off-site. A browser resolves "/\evil.test/x",
// "/<TAB>/evil.test" and "/<LF>/evil.test" to https://evil.test/x (it drops tab and newline, and
// reads a backslash as a slash), and React Router falls back to location.assign for an
// off-origin URL - so every one of them must read as "not a path of ours".
import { describe, expect, it } from "vitest";
import { isSafePath, loginHref, safeNext, safePath, signupHref } from "./auth";

/** What the login loader really receives: the value AFTER `searchParams` decoded the query once. */
const arrivesAs = (query: string) =>
  new URL(`https://app.test/login?next=${query}`).searchParams.get("next");

const HOSTILE = [
  "//evil.test/x",
  "/\\evil.test",
  "/\\evil.test/x",
  "/\t/evil.test",
  "/\n/evil.test",
  "/\r/evil.test",
  "/ /evil.test",
  "/%09/evil.test", // decodes to /TAB/evil.test
  "/%0a/evil.test",
  "/%5Cevil.test", // decodes to /\evil.test
  "/%2F/evil.test", // decodes to ///evil.test
  "/.//evil.test", // dot-segment removal leaves //evil.test
  "https://evil.test",
  "javascript:alert(1)",
  "join/si_abc",
  "",
  // control characters: a NUL makes the redirect's Location header throw, and a browser drops
  // others silently - none is part of a path of ours, raw or one decode later
  "/join/si_abc\u0000",
  "/\u0000/evil.test",
  "/tasks/1\u001b[2J",
  "/projects/x\u007f",
  "/%00/evil.test", // decodes to /NUL/evil.test
  "/join/si_abc%00",
  "/a%1Fb",
  "/a%7Fb",
] as const;

describe("isSafePath", () => {
  it("accepts a same-origin path, its query and its hash", () => {
    for (const ok of [
      "/",
      "/join/si_abc",
      "/projects/x?tab=settings",
      "/tasks/1#top",
      "/a%20b",
      "/100%",
    ]) {
      expect(isSafePath(ok), ok).toBe(true);
    }
  });

  it("refuses everything a browser could resolve off-site", () => {
    for (const bad of HOSTILE) expect(isSafePath(bad), JSON.stringify(bad)).toBe(false);
    expect(isSafePath(null)).toBe(false);
    expect(isSafePath(undefined)).toBe(false);
  });
});

describe("safeNext", () => {
  it("returns a same-origin path as given, so sign-in lands back on the link", () => {
    expect(safeNext("/join/si_abc")).toBe("/join/si_abc");
    expect(safeNext("/projects/x?tab=settings")).toBe("/projects/x?tab=settings");
  });

  it("goes home for anything that is not a same-origin path", () => {
    for (const bad of HOSTILE) expect(safeNext(bad), JSON.stringify(bad)).toBe("/");
    expect(safeNext(null)).toBe("/");
    expect(safeNext(undefined)).toBe("/");
  });

  it("holds for the value as the login loader receives it", () => {
    expect(safeNext(arrivesAs("%2Fjoin%2Fsi_abc"))).toBe("/join/si_abc");
    expect(safeNext(arrivesAs("%2F%5Cevil.test"))).toBe("/"); // arrives as /\evil.test
    expect(safeNext(arrivesAs("%2F%09%2Fevil.test"))).toBe("/"); // arrives as /TAB/evil.test
    expect(safeNext(arrivesAs("%2F%0A%2Fevil.test"))).toBe("/");
    expect(safeNext(arrivesAs("%252F%255Cevil.test"))).toBe("/"); // double-encoded: not a path
    expect(safeNext(arrivesAs("%2F%2Fevil.test"))).toBe("/");
    expect(safeNext(arrivesAs("%2Fjoin%2Fsi_abc%00"))).toBe("/"); // arrives with a NUL
  });
});

describe("safePath - what a redirect may carry", () => {
  const ascii = /^[\x21-\x7e]*$/;

  it("writes a non-ASCII path back percent-encoded, so the Location header cannot throw", () => {
    const arrived = arrivesAs("%2F%E6%97%A5"); // the loader sees "/日"
    expect(arrived).toBe("/\u65e5");
    const out = safeNext(arrived);
    expect(out).toBe("/%E6%97%A5");
    expect(out).toMatch(ascii);
    expect(() => new Headers({ Location: out })).not.toThrow();
    expect(loginHref(arrived ?? undefined)).toBe("/login?next=%2F%25E6%2597%25A5");
  });

  it("keeps a path of ours as it was: path, query and hash", () => {
    for (const ok of ["/join/si_abc", "/projects/x?tab=settings", "/tasks/1#top", "/a%20b"]) {
      expect(safePath(ok), ok).toBe(ok);
    }
    expect(safePath("//evil.test")).toBeNull();
  });
});

describe("loginHref", () => {
  it("carries a join link through sign-in, encoded", () => {
    expect(loginHref("/join/si_abc")).toBe("/login?next=%2Fjoin%2Fsi_abc");
  });

  it("carries nothing when there is nowhere to come back to", () => {
    for (const p of [undefined, "", "/", "/login", "/signup"]) {
      expect(loginHref(p), String(p)).toBe("/login");
    }
  });

  it("never carries a hostile path into the login URL", () => {
    for (const bad of HOSTILE) expect(loginHref(bad), JSON.stringify(bad)).toBe("/login");
  });
});

describe("signupHref", () => {
  it("carries the same next the login page holds, so signing up lands back on the link", () => {
    expect(signupHref("/join/si_abc")).toBe("/signup?next=%2Fjoin%2Fsi_abc");
  });

  it("carries nothing for no next or a hostile one", () => {
    for (const p of [undefined, "", "/", ...HOSTILE]) {
      expect(signupHref(p), JSON.stringify(p)).toBe("/signup");
    }
  });
});
