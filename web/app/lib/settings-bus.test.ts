/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/** The settings bus - a module-level EventTarget precisely so this suite runs in plain node:
 *  dispatch → listener with payload, the default section, unsubscribe, and the connectors
 *  return channel (the roster-changed signal the task page's dial refetches on). */

import { describe, expect, it, vi } from "vitest";
import {
  emitConnectorsChanged,
  onConnectorsChanged,
  onOpenSettings,
  openSettings,
} from "./settings-bus";

describe("open-settings", () => {
  it("delivers the section, defaults to general, and stops after unsubscribe", () => {
    const seen: string[] = [];
    const off = onOpenSettings((s) => seen.push(s));
    openSettings("connectors");
    openSettings(); // no argument - the general tab
    expect(seen).toEqual(["connectors", "general"]);
    off();
    openSettings("connectors");
    expect(seen).toEqual(["connectors", "general"]);
  });

  it("fans out to every subscriber independently", () => {
    const a = vi.fn();
    const b = vi.fn();
    const offA = onOpenSettings(a);
    const offB = onOpenSettings(b);
    openSettings("connectors");
    expect(a).toHaveBeenCalledTimes(1);
    expect(b).toHaveBeenCalledTimes(1);
    offA();
    openSettings("connectors");
    expect(a).toHaveBeenCalledTimes(1);
    expect(b).toHaveBeenCalledTimes(2);
    offB();
  });
});

describe("connectors-changed", () => {
  it("announces with no payload and honors unsubscribe", () => {
    const heard = vi.fn();
    const off = onConnectorsChanged(heard);
    emitConnectorsChanged();
    emitConnectorsChanged();
    expect(heard).toHaveBeenCalledTimes(2);
    off();
    emitConnectorsChanged();
    expect(heard).toHaveBeenCalledTimes(2);
  });

  it("stays out of the open-settings channel - two events, two audiences", () => {
    const opened = vi.fn();
    const changed = vi.fn();
    const offOpen = onOpenSettings(opened);
    const offChanged = onConnectorsChanged(changed);
    emitConnectorsChanged();
    expect(opened).not.toHaveBeenCalled();
    openSettings("connectors");
    expect(changed).toHaveBeenCalledTimes(1);
    offOpen();
    offChanged();
  });
});
