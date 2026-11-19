/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The dataset renderer's contract (ADR-053): records become a table, malformed lines stay
// visible as written, the cap is named - never a wall of raw JSON, never silent loss.
import { DatasetView, parseJsonl } from "./dataset-view";
import type { ArtifactOut, DownloadOut } from "./registry";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

const artifact = (filename: string) => ({ filename }) as unknown as ArtifactOut;
const download = {} as DownloadOut;

describe("parseJsonl", () => {
  it("unions keys across records and keeps column order of first sight", () => {
    const p = parseJsonl('{"a":1,"b":"x"}\n{"b":"y","c":true}\n', 10);
    expect(p.columns).toEqual(["a", "b", "c"]);
    expect(p.rows).toEqual([
      ["1", "x", ""],
      ["", "y", "true"],
    ]);
    expect(p.total).toBe(2);
  });

  it("a line that refuses to parse lands in stray, never dropped", () => {
    const p = parseJsonl('{"a":1}\nnot json at all\n[1,2]\n', 10);
    expect(p.rows).toHaveLength(1);
    expect(p.stray).toEqual(["not json at all", "[1,2]"]);
  });

  it("strips the Excel BOM so the first record parses instead of landing in stray", () => {
    const p = parseJsonl('\uFEFF{"a":1}\n{"a":2}\n', 10);
    expect(p.stray).toEqual([]);
    expect(p.rows).toEqual([["1"], ["2"]]);
    expect(p.total).toBe(2);
  });

  it("spreads an array-of-objects line into records - the whole-file-as-array shape included", () => {
    const p = parseJsonl('[{"a":1},{"a":2,"b":"x"}]\n', 10);
    expect(p.stray).toEqual([]);
    expect(p.columns).toEqual(["a", "b"]);
    expect(p.rows).toEqual([
      ["1", ""],
      ["2", "x"],
    ]);
    expect(p.total).toBe(2);
  });

  it("counts records and strays honestly past the row cap - arrays included", () => {
    const p = parseJsonl('[{"n":1},{"n":2}]\n{"n":3}\nbroken\n{"n":4}\n', 2);
    expect(p.rows).toHaveLength(2);
    expect(p.total).toBe(4);
    expect(p.stray).toEqual(["broken"]);
  });

  it("unwraps the one-object one-array envelope (scatter's RecordSet) into the real table", () => {
    const line = JSON.stringify({
      seat: "github-stars-high",
      records: [
        { entity: "Milvus", url: "https://github.com/milvus-io/milvus" },
        { entity: "Qdrant", url: "https://github.com/qdrant/qdrant" },
      ],
      exhausted: true,
    });
    const p = parseJsonl(line + "\n", 10);
    expect(p.columns).toEqual(["entity", "url"]);
    expect(p.rows).toHaveLength(2);
    expect(p.total).toBe(2);
    expect(p.caption).toContain("seat: github-stars-high");
    expect(p.caption).toContain("exhausted: true");
  });

  it("caps rows but counts every record honestly", () => {
    const lines = Array.from({ length: 8 }, (_, i) => `{"n":${i}}`).join("\n");
    const p = parseJsonl(lines, 5);
    expect(p.rows).toHaveLength(5);
    expect(p.total).toBe(8);
  });
});

describe("DatasetView", () => {
  it("renders a jsonl dataset as a table with a record count - not a JSON wall", () => {
    const text =
      '{"entity":"Qdrant","license":"Apache-2.0","url":"https://github.com/qdrant/qdrant"}\n';
    const html = renderToStaticMarkup(
      <DatasetView artifact={artifact("records.jsonl")} download={download} text={text} />,
    );
    expect(html).toContain("<th");
    expect(html).toMatch(/<th[^>]*><span[^>]*>entity<\/span><\/th>/);
    expect(html).toContain("Qdrant");
    expect(html).toContain("1 record");
    expect(html).not.toContain("{&quot;entity&quot;");
  });

  it("renders csv with the header row as columns", () => {
    const html = renderToStaticMarkup(
      <DatasetView
        artifact={artifact("rows.csv")}
        download={download}
        text={"name,license\nMilvus,Apache-2.0\n"}
      />,
    );
    expect(html).toMatch(/<th[^>]*><span[^>]*>name<\/span><\/th>/);
    expect(html).toContain("Milvus");
    expect(html).toContain("1 record");
  });

  it("an empty file says so quietly", () => {
    const html = renderToStaticMarkup(
      <DatasetView artifact={artifact("empty.jsonl")} download={download} text="" />,
    );
    expect(html).toContain("The dataset is empty.");
  });

  it("caps a stray line's rendered length and the marker names what it cut", () => {
    const long = "x".repeat(500);
    const html = renderToStaticMarkup(
      <DatasetView
        artifact={artifact("records.jsonl")}
        download={download}
        text={`{"a":1}\n${long}\n`}
      />,
    );
    expect(html).not.toContain(long);
    expect(html).toContain("(+100 chars)");
  });

  it("names the 20-line stray cut instead of claiming all are shown", () => {
    const text = Array.from({ length: 25 }, (_, i) => `broken ${i}`).join("\n");
    const html = renderToStaticMarkup(
      <DatasetView artifact={artifact("records.jsonl")} download={download} text={text} />,
    );
    expect(html).toContain("first 20 of 25 shown");
    expect(html).toContain("broken 19");
    expect(html).not.toContain("broken 20");
    expect(html).not.toContain("shown as written");
  });

  it("keys csv headers by position and caps them - duplicates and runaways are legal CSV", () => {
    const long = "h".repeat(450);
    // React logs duplicate-key errors to console.error; a name-keyed header did exactly that.
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    try {
      const html = renderToStaticMarkup(
        <DatasetView
          artifact={artifact("dup.csv")}
          download={download}
          text={`a,a,${long}\n1,2,3\n`}
        />,
      );
      expect(errors.mock.calls.flat().join(" ")).not.toMatch(/same key/i);
      expect(html).not.toContain(long);
      expect(html).toContain("h".repeat(400) + "…");
    } finally {
      errors.mockRestore();
    }
  });
});
