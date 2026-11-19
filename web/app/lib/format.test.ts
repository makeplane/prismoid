/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { describe, expect, it } from "vitest";
import { formatUsd } from "./format";

describe("formatUsd", () => {
  it("reads cents from a dollar up and three places below a dime", () => {
    expect(formatUsd(12.3456)).toBe("$12.35");
    expect(formatUsd(0.25)).toBe("$0.25");
    expect(formatUsd(0.0471)).toBe("$0.047");
    expect(formatUsd(0.0012)).toBe("$0.001");
  });

  it("a sliver reads as under a tenth of a cent, never six decimals", () => {
    // Regression: the topbar showed "$0.000164".
    expect(formatUsd(0.000164)).toBe("<$0.001");
  });

  it("zero and negatives stay honest", () => {
    expect(formatUsd(0)).toBe("$0.00");
    expect(formatUsd(-1.5)).toBe("-$1.50");
  });
});
