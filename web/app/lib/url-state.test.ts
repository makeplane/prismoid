/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The pure half of the URL-state helpers. `useUrlParam`/`useUrlTab` are hooks over
// react-router's own store and are exercised by the screens; `activeNavPath` is arithmetic on
// strings, it is now shared by two applications' sidebars, and it is exactly the kind of thing
// that goes subtly wrong when a route is nested one level deeper.

import { describe, expect, it } from "vitest";
import { activeNavPath } from "./url-state";

const NAV = ["/", "/users", "/projects", "/tasks", "/bots"];

describe("activeNavPath", () => {
  it("lights the entry a child route belongs to", () => {
    expect(activeNavPath("/users/abc-123", NAV)).toBe("/users");
    expect(activeNavPath("/users/abc-123?tab=access", NAV)).toBe("/users");
    expect(activeNavPath("/users", NAV)).toBe("/users");
  });

  it("keeps the landing entry from swallowing every path", () => {
    expect(activeNavPath("/", NAV)).toBe("/");
    expect(activeNavPath("/tasks", NAV)).toBe("/tasks");
  });

  it("prefers the LONGEST match when entries nest", () => {
    const nested = ["/projects", "/projects/archived"];
    expect(activeNavPath("/projects/archived/x", nested)).toBe("/projects/archived");
    expect(activeNavPath("/projects/x", nested)).toBe("/projects");
  });

  it("matches on segment boundaries, not on string prefixes", () => {
    // "/bots-of-theseus" starts with "/bots" and is a different section entirely.
    expect(activeNavPath("/bots-of-theseus", NAV)).toBe("");
  });

  it("returns nothing for a path no entry owns", () => {
    expect(activeNavPath("/nowhere", NAV)).toBe("");
    expect(activeNavPath("/users", [])).toBe("");
  });
});
