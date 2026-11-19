/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The `change` viewer: the report, the diff and the screenshots of a
// coding task on the device. Everything in the archive is the agent's, so the pins that matter
// are what it may NOT become: HTML from a diff, a link to a file, an image the reader never chose.
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import {
  MAX_MEMBERS,
  MAX_TEXT_BYTES,
  parseChange,
  readTar,
  type ChangeArchive,
} from "./change-archive";
import { ChangeBody } from "./change-view";
import { rendererFor } from "./registry";

const PNG = Uint8Array.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 0, 0, 0]);
const encoder = new TextEncoder();

function header(name: string, size: number, type = "0"): Uint8Array {
  const block = new Uint8Array(512);
  block.set(encoder.encode(name), 0);
  block.set(encoder.encode("0000644\0"), 100);
  block.set(encoder.encode(size.toString(8).padStart(11, "0") + "\0"), 124);
  block.set(encoder.encode("        "), 148);
  block.set(encoder.encode(type), 156);
  block.set(encoder.encode("ustar\0" + "00"), 257);
  const sum = block.reduce((total, byte) => total + byte, 0);
  block.set(encoder.encode(sum.toString(8).padStart(6, "0") + "\0 "), 148);
  return block;
}

function tar(entries: [string, Uint8Array | string, string?][]): ArrayBuffer {
  const parts: Uint8Array[] = [];
  for (const [name, content, type] of entries) {
    const body = typeof content === "string" ? encoder.encode(content) : content;
    parts.push(
      header(name, body.length, type),
      body,
      new Uint8Array((512 - (body.length % 512)) % 512),
    );
  }
  parts.push(new Uint8Array(1024));
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let at = 0;
  for (const part of parts) {
    out.set(part, at);
    at += part.length;
  }
  return out.buffer;
}

function paxPath(path: string): string {
  const tail = ` path=${path}\n`;
  let length = tail.length + 2;
  while (`${length}${tail}`.length !== length) length = `${length}${tail}`.length;
  return `${length}${tail}`;
}

const whole = (): ArrayBuffer =>
  tar([
    ["change/report.md", "# What changed\n\nThe home page greets you.\n"],
    ["change/patch.diff", "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-<script>old()</script>\n+new\n"],
    ["change/test-output.txt", "12 passed\n"],
    ["change/screenshots/home.png", PNG],
  ]);

function body(
  over: Partial<ChangeArchive> = {},
  shots: { name: string; url: string }[] = [],
): string {
  const change: ChangeArchive = {
    report: "# What changed",
    diff: "+new",
    testOutput: "12 passed",
    screenshots: [],
    notes: [],
    ...over,
  };
  return renderToStaticMarkup(<ChangeBody change={change} screenshots={shots} />);
}

describe("ChangeViewer", () => {
  it("renders report, diff, screenshots", () => {
    const change = parseChange(whole());
    const html = renderToStaticMarkup(
      <ChangeBody
        change={change}
        screenshots={[{ name: "screenshots/home.png", url: "blob:x" }]}
      />,
    );
    expect(html).toContain("<h1>What changed</h1>");
    expect(html).toContain("+new");
    expect(html).toContain("12 passed");
    expect(html).toContain('src="blob:x"');
    expect(html).toContain("screenshots/home.png");
  });

  it("is what a `change` hint opens, and it fetches its own archive", () => {
    expect(rendererFor("change")?.source).toBe("none");
  });

  it("never renders a diff as HTML", () => {
    const html = renderToStaticMarkup(
      <ChangeBody change={parseChange(whole())} screenshots={[]} />,
    );
    expect(html).not.toContain("<script>");
    expect(html).toContain("&lt;script&gt;");
  });

  it("turns no path in the report into a link", () => {
    const html = body({
      report:
        "[keys](file:///Users/me/.ssh/id) [src](/Users/me/code/app.py) [rel](src/app.py) [up](../x) " +
        "<a href='/Users/me'>raw</a> /Users/me/code/app.py ![shot](/Users/me/a.png)",
    });
    expect(html).not.toMatch(/href="(?!https?:)/);
    expect(html).not.toContain("<img");
    expect(html).toContain("keys");
  });

  it("keeps a web link in the report, leaving safely", () => {
    const html = body({ report: "[docs](https://example.com/a)" });
    expect(html).toContain('href="https://example.com/a"');
    expect(html).toContain('rel="noopener noreferrer"');
  });

  it("loads no image the report names, off any origin", () => {
    expect(body({ report: "![x](https://evil.example/beacon.png)" })).not.toContain('beacon.png"');
    expect(body({ report: "![x](https://evil.example/beacon.png)" })).not.toContain("<img");
  });

  it("says when a piece is missing", () => {
    const html = body({ report: null, diff: null, testOutput: null });
    expect(html).toContain("no report");
    expect(html).toContain("no patch");
    expect(html).toContain("no test output");
  });

  it("says what it cut", () => {
    expect(body({ notes: ["patch.diff: the first 1 MiB is shown"] })).toContain(
      "the first 1 MiB is shown",
    );
  });
});

describe("parseChange", () => {
  it("reads the pieces out of publish's wrapping folder", () => {
    const change = parseChange(whole());
    expect(change.report).toContain("greets you");
    expect(change.diff).toContain("+new");
    expect(change.testOutput).toBe("12 passed\n");
    expect(change.screenshots.map((s) => s.name)).toEqual(["screenshots/home.png"]);
  });

  it("reads a flat archive too", () => {
    expect(parseChange(tar([["report.md", "hi"]])).report).toBe("hi");
  });

  it("follows a long name in a pax header", () => {
    const name = `change/screenshots/${"a".repeat(120)}.png`;
    const change = parseChange(
      tar([
        ["change/report.md", "hi"],
        ["PaxHeaders/x", paxPath(name), "x"],
        ["change/screenshots/trunc", PNG],
      ]),
    );
    expect(change.screenshots.map((s) => s.name)).toEqual([name.slice("change/".length)]);
  });

  it("drops a name that climbs, and anything but a regular file", () => {
    const members = readTar(
      tar([
        ["../report.md", "a"],
        ["/etc/passwd", "b"],
        ["a/../../c", "c"],
        ["link", "", "2"],
        ["dir/", "", "5"],
        ["kept.txt", "d"],
      ]),
    );
    expect([...members.keys()]).toEqual(["kept.txt"]);
  });

  it("shows only what is a PNG", () => {
    const change = parseChange(
      tar([
        ["change/screenshots/fake.png", "<svg onload=alert(1)>"],
        ["change/screenshots/real.png", PNG],
      ]),
    );
    expect(change.screenshots.map((s) => s.name)).toEqual(["screenshots/real.png"]);
    expect(change.notes.join(" ")).toContain("1 screenshot");
  });

  it("caps what it holds and says so", () => {
    const change = parseChange(tar([["change/patch.diff", "x".repeat(MAX_TEXT_BYTES + 5)]]));
    expect(change.diff?.length).toBe(MAX_TEXT_BYTES);
    expect(change.notes.join(" ")).toContain("patch.diff");
  });

  it("says when the archive was longer than the member cap", () => {
    const entries: [string, string][] = Array.from({ length: MAX_MEMBERS + 2 }, (_, i) => [
      `junk/${i}`,
      "",
    ]);
    expect(parseChange(tar(entries)).notes.join(" ")).toContain("members");
  });

  it("refuses bytes that are not a tar", () => {
    expect(() => parseChange(new TextEncoder().encode("x".repeat(2048)).buffer)).toThrow();
  });
});
