/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { describe, expect, it } from "vitest";
import {
  HOLD_PAGE,
  holdsPage,
  pendingPath,
  revalidateOnPageChange,
  revalidateShell,
} from "./pending-route";

describe("pendingPath", () => {
  it("names the destination while its loader is out", () => {
    expect(pendingPath({ state: "loading", location: { pathname: "/tasks" } }, "/")).toBe("/tasks");
  });

  it("keeps the page on screen when nothing is loading", () => {
    expect(pendingPath({ state: "idle" }, "/tasks")).toBeNull();
  });

  it("ignores a navigation that stays on the same page", () => {
    // a filter or a tab rides the query string: the page is already there
    expect(
      pendingPath({ state: "loading", location: { pathname: "/tasks" } }, "/tasks"),
    ).toBeNull();
  });

  it("ignores a submit and the reload that follows it", () => {
    const location = { pathname: "/login" };
    expect(pendingPath({ state: "submitting", location, formMethod: "POST" }, "/")).toBeNull();
    expect(pendingPath({ state: "loading", location, formMethod: "POST" }, "/")).toBeNull();
  });
});

describe("holdsPage", () => {
  it("is true only for a navigation that carries the hold", () => {
    const to = (state?: unknown) => ({
      state: "loading" as const,
      location: { pathname: "/t", state },
    });
    expect(holdsPage(to(HOLD_PAGE))).toBe(true);
    expect(holdsPage(to())).toBe(false);
    expect(holdsPage(to(null))).toBe(false);
    expect(holdsPage(to("holdPage"))).toBe(false);
    expect(holdsPage({ state: "idle" })).toBe(false);
  });
});

describe("revalidateOnPageChange", () => {
  const at = (pathname: string, search = "") => ({ pathname, search });
  const ask = (from: ReturnType<typeof at>, to: ReturnType<typeof at>, formMethod?: string) =>
    revalidateOnPageChange({
      currentUrl: from,
      nextUrl: to,
      formMethod,
      defaultShouldRevalidate: true,
    });

  it("skips the loader for a tab, a filter or a peek in the query string", () => {
    expect(ask(at("/routines"), at("/routines", "?routine=r1"))).toBe(false);
    expect(ask(at("/bots/a", "?tab=runs"), at("/bots/a"))).toBe(false);
  });

  it("keeps the router's answer for a change of page", () => {
    expect(ask(at("/routines", "?routine=r1"), at("/tasks"))).toBe(true);
  });

  it("keeps the router's answer for an asked-for reload and for a submit", () => {
    expect(ask(at("/routines", "?routine=r1"), at("/routines", "?routine=r1"))).toBe(true);
    expect(ask(at("/login"), at("/login", "?next=/"), "POST")).toBe(true);
  });

  it("never turns a skip into a load", () => {
    expect(
      revalidateOnPageChange({
        currentUrl: at("/a"),
        nextUrl: at("/b"),
        defaultShouldRevalidate: false,
      }),
    ).toBe(false);
  });
});

describe("revalidateShell", () => {
  const at = (pathname: string, search = "") => ({ pathname, search });
  const ask = (from: ReturnType<typeof at>, to: ReturnType<typeof at>, formMethod?: string) =>
    revalidateShell({ currentUrl: from, nextUrl: to, formMethod, defaultShouldRevalidate: true });

  it("does not reload for a query string that differs, on the same page or across pages", () => {
    expect(ask(at("/bots/a"), at("/bots/a", "?tab=runs"))).toBe(false);
    expect(ask(at("/bots/a", "?tab=configure"), at("/bots/b"))).toBe(false);
  });

  it("keeps the router's answer for an asked-for reload and for a submit", () => {
    expect(ask(at("/inbox"), at("/inbox"))).toBe(true);
    expect(ask(at("/a", "?x=1"), at("/b"), "POST")).toBe(true);
  });
});
