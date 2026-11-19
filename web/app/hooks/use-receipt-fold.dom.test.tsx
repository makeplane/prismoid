/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// The receipt fold: reveal opens it, and a different artifact starts folded again.
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { useReceiptFold } from "./use-receipt-fold";

afterEach(cleanup);

describe("useReceiptFold", () => {
  it("opens on reveal and folds again for another artifact", () => {
    const { result, rerender } = renderHook(({ id }) => useReceiptFold(id), {
      initialProps: { id: "a1" },
    });
    expect(result.current.open).toBe(false);
    act(() => result.current.reveal());
    expect(result.current.open).toBe(true);
    rerender({ id: "a2" });
    expect(result.current.open).toBe(false);
  });
});
