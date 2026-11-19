/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The CSV preview core (ADR-053): RFC-4180 quoting without a dependency, an honest cap,
// and the streaming trim that holds back a half-typed row.
import { describe, expect, it } from "vitest";
import { parseCsv, trimOpenTail } from "./csv";

describe("parseCsv", () => {
  it("reads quoted fields with embedded commas, quotes, and newlines", () => {
    const { rows } = parseCsv('name,note\n"Doe, Jane","She said ""hi""\nand left"\n', 10);
    expect(rows).toEqual([
      ["name", "note"],
      ["Doe, Jane", 'She said "hi"\nand left'],
    ]);
  });

  it("tolerates CRLF and a missing final newline", () => {
    const { rows } = parseCsv("a,b\r\n1,2", 10);
    expect(rows).toEqual([
      ["a", "b"],
      ["1", "2"],
    ]);
  });

  it("caps rows and counts the remainder instead of hiding it", () => {
    const raw = ["h"].concat(Array.from({ length: 60 }, (_, i) => String(i))).join("\n");
    const { rows, more } = parseCsv(raw, 51);
    expect(rows).toHaveLength(51);
    expect(more).toBe(10);
  });

  it("strips the Excel BOM so a quoted first header still opens as quoted", () => {
    const { rows } = parseCsv('\uFEFF"Company, Inc",Revenue\n"Acme, Co",10\n', 10);
    expect(rows).toEqual([
      ["Company, Inc", "Revenue"],
      ["Acme, Co", "10"],
    ]);
  });

  it("skips blank lines and never throws on a malformed tail", () => {
    const { rows } = parseCsv('a,b\n\n"unclosed', 10);
    expect(rows[0]).toEqual(["a", "b"]);
    expect(rows[1]).toEqual(["unclosed"]);
  });
});

describe("trimOpenTail", () => {
  it("holds back the partial trailing line; a closed line stands", () => {
    expect(trimOpenTail("a,b\n1,2\n3,")).toBe("a,b\n1,2\n");
    expect(trimOpenTail("a,b\n1,2\n")).toBe("a,b\n1,2\n");
    expect(trimOpenTail("partial")).toBe("");
  });
});
