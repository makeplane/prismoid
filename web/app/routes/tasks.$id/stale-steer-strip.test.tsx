/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The stale-steer strip (ADR-064): a refused steer names what its author missed, one line per
// change, and offers Send anyway / Discard - the draft is never thrown away unasked.
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { StaleSteerStrip, type StaleSteer } from "./stale-steer-strip";

const strip = (stale: StaleSteer) =>
  renderToStaticMarkup(
    <StaleSteerStrip stale={stale} onSendAnyway={() => {}} onDiscard={() => {}} />,
  );

describe("StaleSteerStrip", () => {
  it("names a steer that landed while you typed, with its words, and a turn that started", () => {
    const html = strip({
      text: "make it shorter",
      missed: [
        {
          kind: "steer",
          author: "Bob",
          text: "use euros",
          epoch: 3,
          state: "accepted",
        },
        { kind: "turn", author: "Ada", epoch: 4 },
      ],
    });
    expect(html).toContain(
      '<span class="text-foreground font-medium">Bob</span> steered while you were typing: &#x27;use euros&#x27;',
    );
    expect(html).toContain(
      '<span class="text-foreground font-medium">Ada</span> started the next turn',
    );
    expect(html).toContain('role="alert"');
  });

  it("offers both ways on: Send anyway and Discard", () => {
    const html = strip({
      text: "x",
      missed: [{ kind: "turn", author: "Ada", epoch: 2 }],
    });
    expect(html).toContain("Send anyway");
    expect(html).toContain("Discard");
  });

  it("says the task moved on even when the server named nothing", () => {
    expect(strip({ text: "x", missed: [] })).toContain("The task moved on while you were typing.");
  });
});
