/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { Bubble, BubbleContent, BubbleGroup, BubbleReactions } from "./index";

afterEach(cleanup);

describe("Bubble", () => {
  it("defaults to the default variant at the start, and wears what it is given", () => {
    render(
      <BubbleGroup data-testid="group">
        <Bubble data-testid="mine" variant="secondary" align="end">
          <BubbleContent>hello</BubbleContent>
        </Bubble>
        <Bubble data-testid="theirs">
          <BubbleContent>hi</BubbleContent>
        </Bubble>
      </BubbleGroup>,
    );
    expect(screen.getByTestId("group").dataset.slot).toBe("bubble-group");
    expect(screen.getByTestId("mine").dataset).toMatchObject({
      slot: "bubble",
      variant: "secondary",
      align: "end",
    });
    expect(screen.getByTestId("theirs").dataset).toMatchObject({
      variant: "default",
      align: "start",
    });
  });

  it("content renders as a div by default, or as the element a render gives it", () => {
    render(
      <>
        <BubbleContent>plain</BubbleContent>
        <BubbleContent render={<button type="button" />}>pressable</BubbleContent>
      </>,
    );
    expect(screen.getByText("plain").tagName).toBe("DIV");
    expect(screen.getByRole("button", { name: "pressable" })).toBeDefined();
  });

  it("reactions sit at the bottom end unless told otherwise", () => {
    render(
      <>
        <BubbleReactions data-testid="default" />
        <BubbleReactions data-testid="top" side="top" align="start" />
      </>,
    );
    expect(screen.getByTestId("default").dataset).toMatchObject({
      slot: "bubble-reactions",
      side: "bottom",
      align: "end",
    });
    expect(screen.getByTestId("top").dataset).toMatchObject({ side: "top", align: "start" });
  });
});
