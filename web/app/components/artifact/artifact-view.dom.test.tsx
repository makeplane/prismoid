/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// The artifact detail, driven: it loads the artifact, its download, history and receipt in one
// go, renders text kinds from the file's own bytes, falls to a download card when there is no
// renderer or the bytes will not load, folds the verification receipt open on click, and says
// so when the artifact cannot load.
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "~/lib/api/client";
import type { components } from "~/lib/api/types.gen";
import { ArtifactView } from "./artifact-view";

type ArtifactOut = components["schemas"]["ArtifactOut"];

const art = (over: Partial<ArtifactOut> = {}) =>
  ({
    id: "a1",
    task_id: "t",
    type: "document",
    title: "Quarterly notes",
    filename: "notes.md",
    render: "markdown",
    lineage_id: "l",
    label: null,
    has_preview: false,
    has_rendition: false,
    bytes: 2048,
    sha256: "aaa",
    revision: 1,
    provenance: { agent: "writer" },
    verdict: "pass",
    created_at: "2026-10-01T10:00:00Z",
    ...over,
  }) as unknown as ArtifactOut;

const VERIFICATION = {
  verdict: "pass",
  suite: "doc-suite",
  attempt: 2,
  producer_model: "model-a",
  producer_family: "fam-a",
  verifier_model: "model-b",
  verifier_family: "fam-b",
  judge_rationale: "Reads **clearly**.",
  checks: [{ name: "has-title", status: "pass", detail: null, expected: "yes", actual: "yes" }],
  algorithm: "ed25519",
  signature: "sig-abc",
  signature_valid: true,
  public_key: "pk-xyz",
};
const DOWNLOAD = { url: "https://files.test/notes.md", filename: "notes.md" };

const reply = (data: unknown) =>
  Promise.resolve({ data, error: undefined, response: new Response(null, { status: 200 }) });
const fail = () => Promise.reject(new Error("nope"));

function serve(
  over: { artifact?: ArtifactOut; verification?: unknown; revisions?: ArtifactOut[] } = {},
) {
  const artifact = over.artifact ?? art();
  return vi.spyOn(api, "GET").mockImplementation(((path: string) => {
    if (path.endsWith("/verification")) {
      return over.verification === null ? fail() : reply(over.verification ?? VERIFICATION);
    }
    if (path.endsWith("/download")) return reply(DOWNLOAD);
    if (path.endsWith("/revisions")) return reply({ items: over.revisions ?? [artifact] });
    return reply(artifact);
  }) as never);
}

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response("# Hello notes\n\nBody text.", { status: 200 })),
  );
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  document.body.innerHTML = "";
});

describe("ArtifactView", () => {
  it("shows the title, facts, a download link, and the markdown bytes", async () => {
    serve();
    render(<ArtifactView artifactId="a1" />);
    expect(await screen.findByRole("heading", { name: "Quarterly notes" })).toBeDefined();
    expect(await screen.findByText("Body text.")).toBeDefined();
    expect(fetch).toHaveBeenCalledWith(DOWNLOAD.url);
    const link = screen.getByRole("button", { name: "Download" });
    expect(link.getAttribute("href")).toBe(DOWNLOAD.url);
    expect(screen.getByText("2.0 KB")).toBeDefined();
  });

  it("reports the download link outward, and a bare chrome skips the header", async () => {
    serve();
    const onDownloadInfo = vi.fn();
    render(<ArtifactView artifactId="a1" chrome="bare" onDownloadInfo={onDownloadInfo} />);
    await screen.findByText("Body text.");
    expect(onDownloadInfo).toHaveBeenCalledWith(DOWNLOAD);
    expect(screen.queryByRole("heading", { name: "Quarterly notes" })).toBeNull();
  });

  it("host actions show while loading and in the header", async () => {
    serve();
    render(<ArtifactView artifactId="a1" hostActions={<button>Close pane</button>} />);
    expect(screen.getByRole("button", { name: "Close pane" })).toBeDefined();
    await screen.findByText("Body text.");
    expect(screen.getByRole("button", { name: "Close pane" })).toBeDefined();
  });

  it("a kind with no renderer lands on the download card", async () => {
    serve({ artifact: art({ render: "none", type: "spreadsheet", filename: "model.xlsx" }) });
    render(<ArtifactView artifactId="a1" />);
    expect(await screen.findByText("model.xlsx")).toBeDefined();
    expect(screen.getByText(/open the workbook in Excel/)).toBeDefined();
  });

  it("bytes that will not load degrade to the download card", async () => {
    serve();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("", { status: 500 })),
    );
    render(<ArtifactView artifactId="a1" />);
    expect(await screen.findByText(/preview couldn't load/)).toBeDefined();
  });

  it("an artifact that cannot load says so", async () => {
    vi.spyOn(api, "GET").mockImplementation((() => fail()) as never);
    render(<ArtifactView artifactId="a1" />);
    expect(await screen.findByText("Couldn't load this artifact.")).toBeDefined();
  });
});

describe("ArtifactView receipt and history", () => {
  it("the verification folds to one line and opens to the full report", async () => {
    const user = userEvent.setup();
    serve();
    render(<ArtifactView artifactId="a1" />);
    const row = await screen.findByRole("button", { name: /doc-suite/ });
    expect(row.textContent).toContain("judged by model-b (another model family)");
    expect(row.textContent).not.toContain("signed");
    expect(screen.queryByText("Produced by")).toBeNull();
    await user.click(row);
    expect(await screen.findByText("Produced by")).toBeDefined();
    expect(screen.getByText("model-b")).toBeDefined();
    expect(screen.getByText("has-title")).toBeDefined();
    expect(screen.getByText("attempt 2")).toBeDefined();
    expect(screen.getByText("valid")).toBeDefined();
    expect(screen.getByText("sig-abc")).toBeDefined();
  });

  it("an unverified artifact says the receipt is still coming", async () => {
    serve({ verification: null });
    render(<ArtifactView artifactId="a1" />);
    expect(await screen.findByText(/Not verified yet/)).toBeDefined();
  });

  it("a verdict that lands while the reader is open re-reads the receipt", async () => {
    const get = serve({ verification: null });
    const { rerender } = render(<ArtifactView artifactId="a1" />);
    expect(await screen.findByText(/Not verified yet/)).toBeDefined();
    get.mockRestore();
    serve();
    rerender(<ArtifactView artifactId="a1" verdict="pass" />);
    await waitFor(() => expect(screen.queryByText(/Not verified yet/)).toBeNull());
  });

  it("viewing an older version refetches it, and the host is not told of a new head", async () => {
    const user = userEvent.setup();
    const v2 = art({ id: "a2", revision: 2, sha256: "bbb" });
    const v1 = art({ id: "a1", revision: 1 });
    const get = serve({ artifact: v2, revisions: [v2, v1] });
    const onArtifactChanged = vi.fn();
    render(<ArtifactView artifactId="a2" onArtifactChanged={onArtifactChanged} />);
    await user.click(await screen.findByRole("button", { name: /History/ }));
    await user.click((await screen.findAllByText(/writer ·/, { selector: "span" }))[1]);
    await waitFor(() =>
      expect(
        get.mock.calls.some(
          (c) =>
            c[0] === "/v1/artifacts/{artifact_id}" &&
            JSON.stringify(c[1]).includes('"artifact_id":"a1"'),
        ),
      ).toBe(true),
    );
    expect(onArtifactChanged).not.toHaveBeenCalled();
  });
});

describe("ArtifactView - the receipt tells the truth", () => {
  it("a failed receipt says so above the content and its link opens the receipt", async () => {
    serve({
      artifact: art({ verdict: "fail" }),
      verification: { ...VERIFICATION, verdict: "fail" },
    });
    render(<ArtifactView artifactId="a1" />);
    await screen.findByText("Body text.");
    expect(screen.getByRole("note").textContent).toContain("Checks found problems");
    expect(screen.queryByText("Judge rationale")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "See the receipt" }));
    expect(await screen.findByText("Judge rationale")).toBeDefined();
  });

  it("with no model judge the receipt says nobody judged, not a blank 'deterministic suite / -'", async () => {
    serve({
      verification: { ...VERIFICATION, verifier_model: null, verifier_family: null },
    });
    render(<ArtifactView artifactId="a1" />);
    await screen.findByText("Body text.");
    expect(screen.getByText(/no model judge/)).toBeDefined();
    await userEvent.click(screen.getByRole("button", { name: /no model judge/ }));
    expect(screen.getByText("No model judge")).toBeDefined();
    expect(screen.getByText("Deterministic checks only")).toBeDefined();
  });

  it("a wall-of-text check shows its counts and folds the rest behind Show details", async () => {
    const wall = Array.from({ length: 40 }, (_, i) => `'claim ${i}': a source was cut`).join("; ");
    serve({
      verification: {
        ...VERIFICATION,
        checks: [
          {
            name: "grounding:claims",
            status: "fail",
            detail: `0 held, 19 failed, 15 unchecked of 34 claims · unchecked (the judge's): ${wall}`,
            expected: "every claim stated by a source it cites",
            actual: wall,
          },
        ],
      },
    });
    render(<ArtifactView artifactId="a1" />);
    await screen.findByText("Body text.");
    await userEvent.click(screen.getByRole("button", { name: /model-b/ }));
    expect(screen.getByText("0 held, 19 failed, 15 unchecked of 34 claims")).toBeDefined();
    expect(screen.queryByText(/claim 39/)).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Show details" }));
    expect(screen.getAllByText(/claim 39/).length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "Hide details" })).toBeDefined();
  });

  it("the pending line speaks plainly - no engine word", async () => {
    serve({ verification: null });
    render(<ArtifactView artifactId="a1" />);
    const line = await screen.findByText(/Not verified yet/);
    expect(line.textContent).not.toMatch(/ADR/);
  });
});
