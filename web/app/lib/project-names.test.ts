/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// A Project's name is what the task page's crumb needs, and the Project page and the sidebar both
// already know it: remembered per session so a task revalidation never re-asks the API.
import { describe, expect, it } from "vitest";
import { forgetProjectName, knownProjectName, rememberProjectName } from "./project-names";

describe("known Project names", () => {
  it("remembers a name and forgets it", () => {
    expect(knownProjectName("sn-1")).toBeUndefined();
    rememberProjectName("sn-1", "Launch");
    expect(knownProjectName("sn-1")).toBe("Launch");
    forgetProjectName("sn-1");
    expect(knownProjectName("sn-1")).toBeUndefined();
  });

  it("a rename overwrites", () => {
    rememberProjectName("sn-2", "Launch");
    rememberProjectName("sn-2", "Launch 2");
    expect(knownProjectName("sn-2")).toBe("Launch 2");
    forgetProjectName("sn-2");
  });
});
