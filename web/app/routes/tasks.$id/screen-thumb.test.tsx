/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The rail's live thumbnail: who drives, the mask, and a click that opens the pane.
// The socket is the hook's (tested in hooks/use-screen.test.ts); here it is a fixed snapshot.
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ScreenSnapshot } from "~/hooks/use-screen";

const screen = vi.hoisted(() => ({
  value: {} as Partial<ScreenSnapshot>,
  calls: [] as unknown[][],
}));
vi.mock("~/hooks/use-screen", () => ({
  useScreen: (...args: unknown[]) => {
    screen.calls.push(args);
    return {
      frame: null,
      size: null,
      status: "live",
      masked: null,
      busy: false,
      conn: "tthumb0001",
      send: () => {},
      ...screen.value,
    };
  },
}));

import { ScreenThumb, thumbLine } from "./screen-thumb";

const html = (holderName: string | null = null) =>
  renderToStaticMarkup(<ScreenThumb taskId="t1" holderName={holderName} onOpen={() => {}} />);

beforeEach(() => {
  screen.value = {};
  screen.calls = [];
});

describe("ScreenThumb", () => {
  it("opens its own socket as a thumbnail (1 fps), and is a button that opens the pane", () => {
    const out = html();
    expect(screen.calls[0]).toEqual(["t1", { pane: false, enabled: true }]);
    expect(out).toContain('aria-label="Open the screen"');
    expect(out).toContain("<canvas");
  });

  it("says the agent drives, or names the holder", () => {
    expect(html()).toContain("Live · agent driving");
    expect(html("Ada")).toContain("Ada is driving");
  });

  it("with no picture its canvas shows nothing of the last one (asleep, off, reset)", () => {
    screen.value = { frame: null, status: "asleep" };
    expect(html()).toMatch(/<canvas[^>]*opacity-0/);
    screen.value = { frame: { width: 1, height: 1, close: () => {} } as unknown as ImageBitmap };
    expect(html()).not.toMatch(/<canvas[^>]*opacity-0/);
  });

  it("dims the last frame and names the private holder while masked", () => {
    screen.value = {
      masked: "Bob",
      frame: { width: 1, height: 1, close: () => {} } as unknown as ImageBitmap,
    };
    const out = html("Bob");
    expect(out).toContain("Bob is signing in privately");
    expect(out).toContain("opacity-40");
  });

  it("says why there is no picture: starting, off", () => {
    screen.value = { status: "starting" };
    expect(html()).toContain("Starting the browser");
    screen.value = { status: "off" };
    expect(html()).toContain("Computer use is off.");
    screen.value = { status: "stopped" };
    expect(html()).toContain("The browser is stopped. It starts with the agent&#x27;s next step.");
    screen.value = { status: "no_screen" };
    expect(html()).toContain("This Machine has no live screen yet.");
  });
});

describe("an unnamed holder", () => {
  it("is someone, never an empty name", () => {
    expect(thumbLine({ status: "live", masked: "", holderName: "" })).toBe(
      "Someone is signing in privately",
    );
    expect(thumbLine({ status: "live", masked: null, holderName: "" })).toBe("Someone is driving");
  });
});

describe("thumbLine", () => {
  it("the mask outranks the holder, and a status outranks both names", () => {
    expect(thumbLine({ status: "live", masked: "Bob", holderName: "Bob" })).toBe(
      "Bob is signing in privately",
    );
    expect(thumbLine({ status: "connecting", masked: null, holderName: "Ada" })).toBe("Connecting");
  });
});

describe("a settled task's thumbnail", () => {
  it("never says the browser is starting once the task has ended", () => {
    screen.value = { status: "starting" };
    const settled = renderToStaticMarkup(
      <ScreenThumb taskId="t1" holderName={null} settled onOpen={() => {}} />,
    );
    expect(settled).not.toContain("Starting the browser");
    expect(settled).toContain("The browser is stopped");
    expect(html()).toContain("Starting the browser"); // a live task still does
  });
});
