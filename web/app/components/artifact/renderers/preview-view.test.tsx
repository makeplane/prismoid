/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The render-sidecar viewer (ADR-022): one pin per kind, and the SECURITY pins that keep the
// page iframe safe are the point of this file - a page runs with `sandbox="allow-scripts"` and
// nothing else, behind an injected CSP, and a regression here is a same-origin escape.
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { components } from "~/lib/api/types.gen";
import { PAGE_CSP, withPageCsp } from "./page-csp";
import { PreviewBody } from "./preview-view";

type PreviewOut = components["schemas"]["PreviewOut"];

const preview = (over: Partial<PreviewOut>): PreviewOut =>
  ({
    kind: "sheet",
    sheets: [],
    blocks: [],
    slides: [],
    files: [],
    html: null,
    truncated: false,
    ...over,
  }) as PreviewOut;

describe("PreviewBody", () => {
  it("renders a sheet's computed grid with the header row", () => {
    const html = renderToStaticMarkup(
      <PreviewBody
        preview={preview({
          kind: "sheet",
          sheets: [
            {
              name: "Model",
              dims: "5×2",
              rows: [
                ["item", "amount"],
                ["row0", 10],
                ["Total", 60],
              ],
              truncated: false,
            },
          ],
        })}
      />,
    );
    expect(html).toContain("Model");
    expect(html).toContain("60");
    expect(html).toContain("computed in the sandbox");
  });

  it("renders document blocks with real heading structure and tables", () => {
    const html = renderToStaticMarkup(
      <PreviewBody
        preview={preview({
          kind: "document",
          blocks: [
            { style: "h1", text: "Findings", table: null },
            { style: "p", text: "Revenue grew.", table: null },
            { style: null, text: null, table: [["k", "v"]] },
          ],
        })}
      />,
    );
    expect(html).toContain("Findings");
    expect(html).toContain("Revenue grew.");
    expect(html).toContain("<table");
  });

  it("renders deck slides as numbered cards", () => {
    const html = renderToStaticMarkup(
      <PreviewBody
        preview={preview({
          kind: "deck",
          slides: [{ title: "One point", lines: ["a line"] }],
        })}
      />,
    );
    expect(html).toContain("Slide 1");
    expect(html).toContain("One point");
    expect(html).toContain("a line");
  });

  it("renders code files as monospace source", () => {
    const html = renderToStaticMarkup(
      <PreviewBody
        preview={preview({
          kind: "code",
          files: [{ path: "main.py", size: 12, text: "print('hi')" }],
        })}
      />,
    );
    expect(html).toContain("main.py");
    expect(html).toContain("print(&#x27;hi&#x27;)");
  });

  it("an unknown future kind renders nothing - the DownloadCard floor holds", () => {
    expect(renderToStaticMarkup(<PreviewBody preview={preview({ kind: "hologram" })} />)).toBe("");
  });
});

describe("the page iframe - the security pins (ADR-022)", () => {
  const page = () =>
    renderToStaticMarkup(
      <PreviewBody
        preview={preview({ kind: "page", html: "<!doctype html><head></head><h1>Hi</h1>" })}
      />,
    );

  it("sandboxes with allow-scripts and NOTHING else - never allow-same-origin", () => {
    const html = page();
    expect(html).toContain('sandbox="allow-scripts"');
    expect(html).not.toContain("allow-same-origin");
    expect(html).not.toContain("allow-top-navigation");
    expect(html).not.toContain("allow-popups");
    expect(html).not.toContain("allow-forms");
  });

  it("sends no referrer and injects the no-network CSP into the srcdoc", () => {
    const html = page();
    expect(html).toMatch(/referrerpolicy="no-referrer"/i);
    expect(html).toContain("Content-Security-Policy");
    expect(html).toContain("default-src &#x27;none&#x27;");
  });

  it("offers the source, honestly labelled as sandboxed", () => {
    const html = page();
    expect(html).toContain("View source");
    expect(html).toContain("Running sandboxed");
  });
});

describe("withPageCsp", () => {
  it("injects the CSP meta at the DOCUMENT front, before anything parseable", () => {
    const out = withPageCsp('<html><head data-x="1"><title>T</title></head></html>');
    expect(out.startsWith('<meta http-equiv="Content-Security-Policy"')).toBe(true);
    expect(out.indexOf("Content-Security-Policy")).toBeLessThan(out.indexOf("<title>"));
    expect(out).toContain(PAGE_CSP);
  });

  it("keeps the doctype first - the meta lands right after it", () => {
    const out = withPageCsp("<!doctype html><head></head><h1>Hi</h1>");
    expect(out.startsWith("<!doctype html><meta http-equiv")).toBe(true);
  });

  it("beats content smuggled BEFORE the head - the policy must precede any fetchable tag", () => {
    // A <script src> before a literal <head> is hoisted into the synthesized head; a policy
    // injected after the literal <head> would arrive too late to stop that fetch.
    const out = withPageCsp('<script src="https://evil.example/x.js"></script><head></head>');
    expect(out.indexOf("Content-Security-Policy")).toBeLessThan(out.indexOf("<script"));
  });

  it("fronts the meta when the page has no head at all", () => {
    const out = withPageCsp("<h1>bare</h1>");
    expect(out.startsWith("<meta http-equiv")).toBe(true);
    expect(out).toContain("<h1>bare</h1>");
  });
});
