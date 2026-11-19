/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The new-task chord: Cmd or Ctrl with Shift and O, and nothing that merely resembles it.
import { describe, expect, it } from "vitest";
import { isNewTaskShortcut } from "./new-task-shortcut";

const key = (
  k: string,
  mods: Partial<Record<"metaKey" | "ctrlKey" | "shiftKey" | "altKey", boolean>> = {},
) => ({
  key: k,
  metaKey: false,
  ctrlKey: false,
  shiftKey: false,
  altKey: false,
  ...mods,
});

describe("isNewTaskShortcut", () => {
  it("takes Cmd+Shift+O and Ctrl+Shift+O, in either letter case", () => {
    expect(isNewTaskShortcut(key("O", { metaKey: true, shiftKey: true }))).toBe(true);
    expect(isNewTaskShortcut(key("O", { ctrlKey: true, shiftKey: true }))).toBe(true);
    expect(isNewTaskShortcut(key("o", { metaKey: true, shiftKey: true }))).toBe(true);
  });

  it("ignores a plain O, a missing Shift, an extra Alt and other letters", () => {
    expect(isNewTaskShortcut(key("o"))).toBe(false);
    expect(isNewTaskShortcut(key("O", { shiftKey: true }))).toBe(false);
    expect(isNewTaskShortcut(key("o", { metaKey: true }))).toBe(false);
    expect(isNewTaskShortcut(key("O", { metaKey: true, shiftKey: true, altKey: true }))).toBe(
      false,
    );
    expect(isNewTaskShortcut(key("N", { metaKey: true, shiftKey: true }))).toBe(false);
  });
});
