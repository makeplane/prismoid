/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { Spinner } from "./index";

afterEach(cleanup);

describe("Spinner", () => {
  it("is a status named Loading that spins", () => {
    render(<Spinner />);
    const spinner = screen.getByRole("status", { name: "Loading" });
    expect(spinner.getAttribute("data-slot")).toBe("spinner");
    expect(spinner.getAttribute("class")).toContain("animate-spin");
  });

  it("takes a caller's size class and a caller's name", () => {
    render(<Spinner className="size-6" aria-label="Saving" />);
    const spinner = screen.getByRole("status", { name: "Saving" });
    expect(spinner.getAttribute("class")).toContain("size-6");
    expect(spinner.getAttribute("class")).not.toContain("size-4");
  });
});
