/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "./index";

afterEach(cleanup);

describe("Card", () => {
  it("is default-sized unless told sm, and passes its own props through", () => {
    render(
      <>
        <Card data-testid="card" aria-label="Spend" className="extra" />
        <Card data-testid="small" size="sm" />
      </>,
    );
    const card = screen.getByTestId("card");
    expect(card.dataset).toMatchObject({ slot: "card", size: "default" });
    expect(card.getAttribute("aria-label")).toBe("Spend");
    expect(card.className).toContain("extra");
    expect(screen.getByTestId("small").dataset.size).toBe("sm");
  });

  it("each part names its slot, in the order it was composed", () => {
    render(
      <Card data-testid="card">
        <CardHeader>
          <CardTitle>Spend</CardTitle>
          <CardDescription>This month</CardDescription>
          <CardAction>
            <button type="button">Export</button>
          </CardAction>
        </CardHeader>
        <CardContent>$12</CardContent>
        <CardFooter>Updated now</CardFooter>
      </Card>,
    );
    const slots = [...screen.getByTestId("card").querySelectorAll("[data-slot]")].map(
      (el) => (el as HTMLElement).dataset.slot,
    );
    expect(slots).toEqual([
      "card-header",
      "card-title",
      "card-description",
      "card-action",
      "card-content",
      "card-footer",
    ]);
    expect(screen.getByRole("button", { name: "Export" })).toBeDefined();
  });
});
