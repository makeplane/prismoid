/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The owner as a row of the People roster (ADR-062): the owner is a column on the Project, not
// a member row, so the members list never names them - yet everyone in the Project is owed who owns it.
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { OwnerRow } from "./owner-row";

const html = (owner: { name: string; email: string | null }) =>
  renderToStaticMarkup(<OwnerRow owner={owner} />);

describe("OwnerRow", () => {
  it("names the owner, with their email and the role owner", () => {
    const out = html({ name: "Ada Lovelace", email: "ada@x.co" });
    expect(out).toContain("Ada Lovelace");
    expect(out).toContain("ada@x.co");
    expect(out).toContain(">Owner<"); // the role badge (capitalized in the label: Propel takes no className)
    expect(out).toContain("AL"); // the avatar's initials
  });

  it("falls back to the email when the owner has no display name", () => {
    const out = html({ name: "", email: "ada@x.co" });
    expect(out).toContain("ada@x.co");
    expect(out).not.toContain("Someone");
  });

  it("a workspace guest's read names the owner without an email", () => {
    // the server sends `email: null` to a guest, who knows people by name only
    const out = html({ name: "Ada Lovelace", email: null });
    expect(out).toContain("Ada Lovelace");
    expect(out).toContain("AL");
    expect(out).not.toContain("null");
  });

  it("a name is text, never markup", () => {
    const out = html({ name: "<b>Ada</b>", email: "ada@x.co" });
    expect(out).not.toContain("<b>");
    expect(out).toContain("&lt;b&gt;");
  });
});
