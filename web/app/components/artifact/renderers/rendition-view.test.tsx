/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The full-fidelity rendition viewer (ADR-023): the sheet cage is STRICTER than the page cage
// (a rendition is a static document - the sandbox grants NOTHING), and the PDF path embeds the
// presigned render for the browser's own viewer. Security pins first; loosening them fails CI.
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { RenditionBody } from "./rendition-view";

describe("RenditionBody - html (the sheet cage)", () => {
  const html = renderToStaticMarkup(
    <RenditionBody
      media="text/html"
      html="<html><head></head><table><tr><td>60</td></tr></table></html>"
    />,
  );

  it("sandboxes with NOTHING granted - no scripts, no same-origin, ever", () => {
    expect(html).toContain('sandbox=""');
    expect(html).not.toContain("allow-scripts");
    expect(html).not.toContain("allow-same-origin");
  });

  it("injects the CSP, sends no referrer, and says what it is", () => {
    expect(html).toContain("Content-Security-Policy");
    expect(html).toMatch(/referrerpolicy="no-referrer"/i);
    expect(html).toContain("formulas recalculated");
  });
});

describe("RenditionBody - pdf", () => {
  const html = renderToStaticMarkup(
    <RenditionBody media="application/pdf" url="https://blobs.example/r" />,
  );

  it("embeds the presigned render as application/pdf with an honest fallback", () => {
    expect(html).toContain('type="application/pdf"');
    expect(html).toContain('data="https://blobs.example/r"');
    expect(html).toContain("will not embed");
    expect(html).toContain("Faithful render made in the sandbox");
  });

  it("self-bounds to the documented height precedent - the one-scroll law", () => {
    expect(html).toContain("h-[75dvh]");
    expect(html).toContain("max-h-[56rem]");
  });

  it("uses the DownloadCard as the <object>'s LITERAL fallback child when it has one", () => {
    const artifact = {
      id: "a1",
      type: "document",
      title: "Report",
      filename: "report.pdf",
      bytes: 9,
    } as Parameters<typeof RenditionBody>[0]["artifact"];
    const withCard = renderToStaticMarkup(
      <RenditionBody
        media="application/pdf"
        url="https://blobs.example/r"
        artifact={artifact}
        download={{
          url: "https://blobs.example/dl",
          filename: "report.pdf",
          sha256: "x",
          expires_in: 3600,
        }}
      />,
    );
    expect(withCard).toContain("report.pdf");
    expect(withCard).toContain('href="https://blobs.example/dl"');
    expect(withCard).not.toContain("will not embed");
  });
});
