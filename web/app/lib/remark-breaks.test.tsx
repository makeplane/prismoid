/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { renderToStaticMarkup } from "react-dom/server";
import ReactMarkdown from "react-markdown";
import { describe, expect, it } from "vitest";
import { remarkBreaks } from "./remark-breaks";

const render = (md: string) =>
  renderToStaticMarkup(<ReactMarkdown remarkPlugins={[remarkBreaks]}>{md}</ReactMarkdown>);

describe("remarkBreaks", () => {
  it("keeps each row of a poem on its own line", () => {
    const html = render(
      "Wheels hum a quiet song\nPedals turn as morning breezes blow\nChains and gears",
    );
    expect(html).toBe(
      "<p>Wheels hum a quiet song<br/>\nPedals turn as morning breezes blow<br/>\nChains and gears</p>",
    );
  });

  it("a blank line still starts a new paragraph", () => {
    expect(render("one\n\ntwo")).toBe("<p>one</p>\n<p>two</p>");
  });

  it("breaks inside emphasis and lists too, and drops trailing spaces before the break", () => {
    expect(render("*a  \nb*")).toContain("<em>a<br/>\nb</em>");
    expect(render("- x\n  y")).toContain("x<br/>\ny");
  });

  it("leaves code fences and inline code alone", () => {
    const html = render("```\nline1\nline2\n```");
    expect(html).toContain("line1\nline2");
    expect(html).not.toContain("<br");
  });
});
