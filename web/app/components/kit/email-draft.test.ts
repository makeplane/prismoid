/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The draft grammar's contract (ADR-053): human header lines, a blank separator, a markdown
// body - parsed the same settled or half-streamed, and the clipboard payload carries both
// flavors so a draft pastes rich into a mail composer and honest into a text field.
import { describe, expect, it } from "vitest";
import {
  draftClipboardPayload,
  EMAIL_KEYS,
  escapeHtml,
  parseDraftHeaders,
  parseEmailDraft,
  writeClipboard,
} from "./email-draft";

describe("parseEmailDraft", () => {
  it("reads headers case-insensitively and keeps the body's own markdown", () => {
    const d = parseEmailDraft(
      "TO: alice@example.com\nSubject: The plan\n\nHi Alice,\n\n**Tomorrow.**",
    );
    expect(d.to).toBe("alice@example.com");
    expect(d.subject).toBe("The plan");
    expect(d.body).toBe("Hi Alice,\n\n**Tomorrow.**");
  });

  it("no header lines means the whole fence is body", () => {
    const d = parseEmailDraft("Just a body that starts with prose: not a header.");
    expect(d.to).toBeUndefined();
    expect(d.body).toContain("Just a body");
  });

  it("a URL line is a body, not a header (the colon is not enough)", () => {
    // "https" is not an email key, so parsing stops before it.
    const d = parseEmailDraft("https://example.com is where it lives");
    expect(d.body).toContain("https://example.com");
  });

  it("a repeated header last-wins; unknown keys end the header run", () => {
    const d = parseDraftHeaders("To: a\nTo: b\nX-Weird: nope\nbody", EMAIL_KEYS);
    expect(d.headers.to).toBe("b");
    expect(d.body).toBe("X-Weird: nope\nbody");
  });

  it("tolerates the half-streamed tail: a partial header parses as far as it goes", () => {
    expect(parseEmailDraft("To: ali").to).toBe("ali");
    expect(parseEmailDraft("To: a@b.c\n\nHey th").body).toBe("Hey th");
  });
});

describe("draftClipboardPayload", () => {
  it("carries both flavors: escaped rich headers + verbatim plain draft", () => {
    const p = draftClipboardPayload(
      [
        ["To", "a <b@c.d>"],
        ["Cc", undefined],
        ["Subject", "Q3 & beyond"],
      ],
      "**body**",
      "<p><strong>body</strong></p>",
    );
    expect(p.html).toContain("<b>To:</b> a &lt;b@c.d&gt;");
    expect(p.html).toContain("Q3 &amp; beyond");
    expect(p.html).not.toContain("Cc");
    expect(p.html).toContain("<p><strong>body</strong></p>");
    expect(p.text).toBe("To: a <b@c.d>\nSubject: Q3 & beyond\n\n**body**");
  });

  it("a headerless draft is just the body in both flavors", () => {
    const p = draftClipboardPayload([["To", undefined]], "hi", "hi");
    expect(p.text).toBe("hi");
    expect(p.html).toBe("<div>hi</div>");
  });
});

it("escapeHtml covers the four that matter", () => {
  expect(escapeHtml('<a href="x">&</a>')).toBe("&lt;a href=&quot;x&quot;&gt;&amp;&lt;/a&gt;");
});

describe("writeClipboard", () => {
  it("a missing Clipboard API is a CLEAR rejection, not an undefined dereference", async () => {
    // Node has a `navigator` global but no clipboard on it - exactly the insecure-context
    // shape. The clear throw keeps the caller's check latch from firing on a dead write.
    await expect(writeClipboard({ html: "<div>x</div>", text: "x" })).rejects.toThrow(
      "Clipboard API not available",
    );
  });
});
