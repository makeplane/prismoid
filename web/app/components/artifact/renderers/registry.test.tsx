/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { DownloadCard } from "../download-card";
import { MarkdownView } from "./markdown-view";
import { TextView } from "./text-view";
import { rendererFor, type ArtifactOut, type DownloadOut, RENDERERS } from "./registry";

function artifact(overrides: Partial<ArtifactOut> = {}): ArtifactOut {
  return {
    id: "a-1",
    type: "document",
    title: "A memo",
    filename: "memo.md",
    render: "markdown",
    sha256: "0".repeat(64),
    bytes: 1234,
    provenance: {},
    verdict: null,
    created_at: "2026-07-31T00:00:00Z",
    ...overrides,
  } as ArtifactOut;
}

const download: DownloadOut = {
  url: "https://blobs.example/abc",
  filename: "memo.md",
  sha256: "0".repeat(64),
  expires_in: 3600,
};

describe("rendererFor", () => {
  it("maps the server's text hints to renderers", () => {
    expect(rendererFor("markdown")?.source).toBe("text");
    expect(rendererFor("text")?.source).toBe("text");
  });

  it("maps `pdf` to a no-fetch renderer - the browser embeds the presigned bytes (ADR-022)", () => {
    expect(rendererFor("pdf")?.source).toBe("none");
  });

  it("maps `none` to NO renderer - the mojibake regression", () => {
    // A `.docx`, `.xlsx`, `.pptx`, or code tar arrives with render:"none" (server-derived).
    // No entry means the shell falls to the DownloadCard; nothing can pipe bytes to <Markdown>.
    expect(rendererFor("none")).toBeUndefined();
    expect(rendererFor(undefined)).toBeUndefined();
    expect(rendererFor("some-future-hint")).toBeUndefined();
  });
});

describe("MarkdownView", () => {
  it("renders markdown text", () => {
    const html = renderToStaticMarkup(
      <MarkdownView artifact={artifact()} download={download} text="# Title" />,
    );
    expect(html).toContain("Title");
  });

  it("keeps a hard-wrapped document as written: no line breaks added", () => {
    const html = renderToStaticMarkup(
      <MarkdownView
        artifact={artifact()}
        download={download}
        text={"wrapped by hand\nat a column"}
      />,
    );
    expect(html).toContain("wrapped by hand\nat a column");
    expect(html).not.toContain("<br");
  });
});

describe("TextView", () => {
  it("renders plain text in a mono block and says when it cut", () => {
    const html = renderToStaticMarkup(
      <TextView
        artifact={artifact({ render: "text", filename: "rows.csv", type: "dataset" })}
        download={download}
        text={"a,b\n" + "x,y\n".repeat(40_000)}
      />,
    );
    expect(html).toContain("a,b");
    expect(html).toContain("first 100,000 characters");
  });
});

describe("DownloadCard", () => {
  it("names the file, the kind, and the honest reason there is no preview", () => {
    const html = renderToStaticMarkup(
      <DownloadCard
        artifact={artifact({ type: "spreadsheet", filename: "model.xlsx", render: "none" })}
        download={{ ...download, filename: "model.xlsx" }}
      />,
    );
    expect(html).toContain("model.xlsx");
    expect(html).toContain("Spreadsheet");
    expect(html).toContain("Download to open the workbook");
    expect(html).toContain('href="https://blobs.example/abc"');
  });

  it("says the preview failed when degraded, and survives a missing download", () => {
    const degraded = renderToStaticMarkup(
      <DownloadCard artifact={artifact()} download={download} degraded />,
    );
    expect(degraded).toContain("download the file instead");

    const orphan = renderToStaticMarkup(<DownloadCard artifact={artifact()} download={null} />);
    expect(orphan).toContain("memo.md");
    expect(orphan).not.toContain("href=");
  });
});

describe("the scroll contract (frame)", () => {
  it("EVERY shipped kind reads as a document - no app frames today", () => {
    // Setting frame:"app" bounded the board to the
    // host and it scrolled INSIDE itself - the scroll-in-a-scroll the user named ugly
    // twice. A collection flows at full height like prose now (the page/pane owns the one
    // vertical scroll; the ViewShell pans wide layouts horizontally). The contract stays
    // in the registry for genuinely app-like kinds (a webapp preview); giving it to any
    // current kind resurrects the nested scroll.
    for (const [name, entry] of Object.entries(RENDERERS)) {
      expect(entry.frame, `${name} must flow like a document`).toBeUndefined();
    }
  });
});
