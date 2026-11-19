/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { describe, expect, it } from "vitest";
import { interpretWorkerCheck } from "./worker-liveness";

describe("interpretWorkerCheck", () => {
  it("treats ok as ok", () => {
    expect(interpretWorkerCheck("ok")).toBe("ok");
  });

  it("treats never / stale as offline", () => {
    expect(interpretWorkerCheck("never")).toBe("offline");
    expect(interpretWorkerCheck("stale 200s")).toBe("offline");
  });

  it("claims only what the probe knows: missing or unknown stays unknown", () => {
    expect(interpretWorkerCheck(undefined)).toBe("unknown");
    expect(interpretWorkerCheck("unknown")).toBe("unknown");
  });
});
