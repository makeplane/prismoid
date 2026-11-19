/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The task page's presence chrome: who is here (header), who is typing (above the composer), and
// the viewer's read-only note (in place of the composer).
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { PresenceBar, TypingLine, ViewerNote } from "./presence-bar";

describe("PresenceBar", () => {
  it("editors in full, viewers dimmed as watching", () => {
    const html = renderToStaticMarkup(
      <PresenceBar
        people={[
          { id: "a", name: "Ada", editor: true },
          { id: "c", name: "Cy", editor: false },
        ]}
      />,
    );
    expect(html).toContain('title="Ada"');
    expect(html).toContain('title="Cy (watching)"');
    expect(html).toContain("opacity-50");
  });

  it("you alone on a task is no stack at all - a personal task's header is what it was", () => {
    expect(renderToStaticMarkup(<PresenceBar people={[]} />)).toBe("");
    expect(
      renderToStaticMarkup(<PresenceBar people={[{ id: "a", name: "Ada", editor: true }]} />),
    ).toBe("");
  });
});

describe("TypingLine", () => {
  it("one muted polite status line", () => {
    const html = renderToStaticMarkup(
      <TypingLine people={[{ id: "b", name: "Bob", mode: "steer" }]} />,
    );
    expect(html).toContain("Bob is steering…");
    expect(html).toContain("text-muted-foreground");
    expect(html).toContain('role="status"');
  });

  it("silence keeps the live region MOUNTED and empty - a region added with its text is not announced", () => {
    const html = renderToStaticMarkup(<TypingLine people={[]} />);
    expect(html).toContain('role="status"');
    expect(html).toContain('aria-live="polite"');
    expect(html).toContain("sr-only"); // nothing shows on screen while empty
    expect(html.replace(/<[^>]*>/g, "")).toBe("");
    // and speaking is the same element, no longer sr-only
    const loud = renderToStaticMarkup(
      <TypingLine people={[{ id: "b", name: "Bob", mode: "steer" }]} />,
    );
    expect(loud).not.toContain("sr-only");
  });

  it("a name is text, never markup", () => {
    const html = renderToStaticMarkup(
      <TypingLine people={[{ id: "x", name: "<img src=x onerror=1>", mode: "follow_up" }]} />,
    );
    expect(html).not.toContain("<img");
    expect(html).toContain("&lt;img");
  });
});

describe("ViewerNote", () => {
  it("says what the viewer can do about it", () => {
    const html = renderToStaticMarkup(<ViewerNote />);
    expect(html).toContain("You&#x27;re viewing. Ask the Project owner for edit access.");
    expect(html).not.toContain("<textarea");
  });
});
