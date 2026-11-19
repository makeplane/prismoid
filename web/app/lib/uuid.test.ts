/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// A command id is a UUID the server parses (ADR-064): it must stay UUID-shaped on every origin,
// including a plain-http LAN address where `crypto.randomUUID` does not exist.
import { afterEach, describe, expect, it, vi } from "vitest";
import { uuidV4 } from "./uuid";

const V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("uuidV4", () => {
  it("uses crypto.randomUUID where the context has it", () => {
    const randomUUID = vi.fn(() => "11111111-2222-4333-8444-555555555555");
    vi.stubGlobal("crypto", { randomUUID, getRandomValues: vi.fn() });
    expect(uuidV4()).toBe("11111111-2222-4333-8444-555555555555");
    expect(randomUUID).toHaveBeenCalledOnce();
  });

  it("falls back to getRandomValues in an insecure context - still a v4 UUID", () => {
    vi.stubGlobal("crypto", {
      getRandomValues: (bytes: Uint8Array) => bytes.fill(0xff),
    });
    const id = uuidV4();
    expect(id).toMatch(V4);
    expect(id).toBe("ffffffff-ffff-4fff-bfff-ffffffffffff");
  });

  it("is UUID-shaped even with no crypto at all, and never repeats in practice", () => {
    vi.stubGlobal("crypto", undefined);
    const ids = new Set(Array.from({ length: 50 }, () => uuidV4()));
    for (const id of ids) expect(id).toMatch(V4);
    expect(ids.size).toBe(50);
  });
});
