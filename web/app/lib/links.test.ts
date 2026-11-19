/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// Where a task lives (ADR-062): a Project task at /projects/<sid>/tasks/<tid>,
// a personal task at /tasks/<tid>. The URL's project segment is cosmetic context - the server
// authorizes by task id - so every wrong spelling redirects to the one canonical one.
import { describe, expect, it } from "vitest";
import { canonicalTaskPath, isWithin, projectHref, taskHref, taskRedirect } from "./links";

const S = "5a1d8c52-0f4e-4a54-9d0b-0a3b1d8d2f10";
const T = "62b53b15-c44c-455f-9c78-f752e2bf132e";
const OTHER = "9c9a7a0e-5bd1-45d0-8d15-3c1b8b6f1a77";

describe("taskHref", () => {
  it("a Project task lives under its Project", () => {
    expect(taskHref({ id: T, project_id: S })).toBe(`/projects/${S}/tasks/${T}`);
  });

  it("a personal task stays at /tasks/<id>, whether project_id is null or simply not carried", () => {
    expect(taskHref({ id: T, project_id: null })).toBe(`/tasks/${T}`);
    expect(taskHref({ id: T })).toBe(`/tasks/${T}`);
  });

  it("an empty project id is no Project", () => {
    expect(taskHref({ id: T, project_id: "" })).toBe(`/tasks/${T}`);
  });
});

describe("projectHref and isWithin", () => {
  it("a Project's own page", () => {
    expect(projectHref(S)).toBe(`/projects/${S}`);
  });

  it("a path is within a base when it is the base or below it, never a longer sibling", () => {
    expect(isWithin(`/projects/${S}`, `/projects/${S}`)).toBe(true);
    expect(isWithin(`/projects/${S}/tasks/${T}`, `/projects/${S}`)).toBe(true);
    expect(isWithin(`/projects/${S}x`, `/projects/${S}`)).toBe(false);
    expect(isWithin("/projects", `/projects/${S}`)).toBe(false);
  });
});

describe("canonicalTaskPath - the redirect decision", () => {
  const project = { id: T, project_id: S };
  const personal = { id: T, project_id: null };

  it("/tasks/<id> on a Project task goes to the Project's URL", () => {
    expect(canonicalTaskPath(`/tasks/${T}`, project)).toBe(`/projects/${S}/tasks/${T}`);
  });

  it("the nested URL with the wrong Project (a moved task, a garbage id) goes to the real one", () => {
    expect(canonicalTaskPath(`/projects/${OTHER}/tasks/${T}`, project)).toBe(
      `/projects/${S}/tasks/${T}`,
    );
    expect(canonicalTaskPath(`/projects/not-a-project/tasks/${T}`, project)).toBe(
      `/projects/${S}/tasks/${T}`,
    );
  });

  it("the nested URL on a personal task (moved out of its Project) goes to /tasks/<id>", () => {
    expect(canonicalTaskPath(`/projects/${S}/tasks/${T}`, personal)).toBe(`/tasks/${T}`);
    expect(canonicalTaskPath(`/projects/${S}/tasks/${T}`, { id: T })).toBe(`/tasks/${T}`);
  });

  it("the canonical URL stays", () => {
    expect(canonicalTaskPath(`/projects/${S}/tasks/${T}`, project)).toBeNull();
    expect(canonicalTaskPath(`/tasks/${T}`, personal)).toBeNull();
  });

  it("the query and the fragment never decide, and ride along on a redirect", () => {
    expect(canonicalTaskPath(`/projects/${S}/tasks/${T}?a=1#top`, project)).toBeNull();
    expect(canonicalTaskPath(`/tasks/${T}?a=1&b=2#msg-7`, project)).toBe(
      `/projects/${S}/tasks/${T}?a=1&b=2#msg-7`,
    );
    expect(canonicalTaskPath(`/tasks/${T}#top`, project)).toBe(`/projects/${S}/tasks/${T}#top`);
    expect(canonicalTaskPath(`/projects/${S}/tasks/${T}?a=1`, personal)).toBe(`/tasks/${T}?a=1`);
  });

  it("a trailing slash is the same address", () => {
    expect(canonicalTaskPath(`/tasks/${T}/`, personal)).toBeNull();
    expect(canonicalTaskPath(`/projects/${S}/tasks/${T}/`, project)).toBeNull();
  });
});

// HOW a redirect is made (fix round 1). A redirect that REPLACES the current entry is right only
// when that entry IS the non-canonical URL (a fresh load, Back/Forward onto it, a revalidation):
// otherwise it overwrites the page the reader clicked from, and Back skips it.
describe("taskRedirect - replace or push", () => {
  const project = { id: T, project_id: S };
  const req = { pathname: `/tasks/${T}`, search: "?a=1" };

  it("a fresh load, a popstate or a revalidation (the bar already shows this URL) replaces", () => {
    expect(taskRedirect(req, { pathname: `/tasks/${T}`, hash: "#top" }, project)).toEqual({
      to: `/projects/${S}/tasks/${T}?a=1#top`,
      replace: true,
    });
  });

  it("an in-app click from another page pushes, so history is [origin, canonical]", () => {
    expect(taskRedirect(req, { pathname: "/bots/foo", hash: "#x" }, project)).toEqual({
      // the bar's #x is the ORIGIN's, never this navigation's: dropped
      to: `/projects/${S}/tasks/${T}?a=1`,
      replace: false,
    });
  });

  it("with no window to ask it cannot prove the entry is this URL, so it pushes", () => {
    expect(taskRedirect(req, null, project)).toEqual({
      to: `/projects/${S}/tasks/${T}?a=1`,
      replace: false,
    });
  });

  it("the canonical URL needs no redirect either way", () => {
    const here = { pathname: `/projects/${S}/tasks/${T}`, search: "" };
    expect(taskRedirect(here, { pathname: here.pathname, hash: "" }, project)).toBeNull();
    expect(taskRedirect(here, { pathname: "/inbox", hash: "" }, project)).toBeNull();
  });

  it("a nested URL on a task moved home redirects to /tasks/<id> by the same rule", () => {
    const nested = { pathname: `/projects/${S}/tasks/${T}`, search: "" };
    expect(taskRedirect(nested, { pathname: nested.pathname, hash: "" }, { id: T })).toEqual({
      to: `/tasks/${T}`,
      replace: true,
    });
    expect(taskRedirect(nested, { pathname: "/projects", hash: "" }, { id: T })).toEqual({
      to: `/tasks/${T}`,
      replace: false,
    });
  });
});
