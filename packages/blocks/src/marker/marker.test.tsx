/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { Marker, MarkerContent, MarkerIcon, markerVariants } from "./index";

afterEach(cleanup);

describe("Marker", () => {
  it("renders a div with its icon hidden from assistive tech and its content readable", () => {
    render(
      <Marker data-testid="m">
        <MarkerIcon data-testid="icon">*</MarkerIcon>
        <MarkerContent>Yesterday</MarkerContent>
      </Marker>,
    );
    expect(screen.getByTestId("m").tagName).toBe("DIV");
    expect(screen.getByTestId("icon").getAttribute("aria-hidden")).toBe("true");
    expect(screen.getByText("Yesterday").dataset.slot).toBe("marker-content");
  });

  it("renders as the element a render gives it, a link here", () => {
    render(<Marker render={<a href="/task/1" />}>Open the task</Marker>);
    expect(screen.getByRole("link", { name: "Open the task" }).getAttribute("href")).toBe(
      "/task/1",
    );
  });

  it("each variant adds its own rule: a separator draws lines, a border draws one beneath", () => {
    expect(markerVariants({ variant: "separator" })).toContain("before:border-t");
    expect(markerVariants({ variant: "border" })).toContain("border-b");
    expect(markerVariants({ variant: "default" })).not.toContain("border-b");
  });
});
