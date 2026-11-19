/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import {
  MessageScroller,
  MessageScrollerButton,
  MessageScrollerContent,
  MessageScrollerItem,
  MessageScrollerProvider,
  MessageScrollerViewport,
} from "./index";

afterEach(cleanup);

function Thread({ label }: { label?: string }) {
  return (
    <MessageScrollerProvider>
      <MessageScroller data-testid="root">
        <MessageScrollerViewport data-testid="viewport">
          <MessageScrollerContent data-testid="content">
            <MessageScrollerItem>first message</MessageScrollerItem>
            <MessageScrollerItem scrollAnchor>last message</MessageScrollerItem>
          </MessageScrollerContent>
        </MessageScrollerViewport>
        <MessageScrollerButton aria-label={label} />
        <MessageScrollerButton direction="start" />
      </MessageScroller>
    </MessageScrollerProvider>
  );
}

describe("MessageScroller", () => {
  it("lays the thread out root, viewport, content, items, each naming its slot", () => {
    render(<Thread />);
    expect(screen.getByTestId("root").dataset.slot).toBe("message-scroller");
    expect(screen.getByTestId("viewport").dataset.slot).toBe("message-scroller-viewport");
    expect(screen.getByTestId("content").dataset.slot).toBe("message-scroller-content");
    const items = screen.getAllByText(/message$/);
    expect(items.map((el) => el.dataset.slot)).toEqual([
      "message-scroller-item",
      "message-scroller-item",
    ]);
  });

  it("its jump buttons are named by direction, and a caller can rename one", () => {
    const { unmount } = render(<Thread />);
    expect(screen.getByRole("button", { name: "Scroll to end", hidden: true })).toBeDefined();
    expect(screen.getByRole("button", { name: "Scroll to start", hidden: true })).toBeDefined();
    unmount();
    render(<Thread label="Jump to the newest reply" />);
    expect(
      screen.getByRole("button", { name: "Jump to the newest reply", hidden: true }),
    ).toBeDefined();
  });
});
