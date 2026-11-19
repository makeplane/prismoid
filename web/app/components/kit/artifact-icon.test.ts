/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The naming law, pinned: one vocabulary home, FLAT. One wire kind, one word - no filename
// may change what a kind is called. A drift here is a product wording bug on every chip,
// card, dial, and rail at once.
import { describe, expect, it } from "vitest";
import { AppWindow, File, FileText, Presentation } from "lucide-react";
import { artifactIcon, artifactKindLabel } from "./artifact-icon";

describe("artifactKindLabel - the naming law", () => {
  it("a document is a Document - markdown, Word, PDF, or HTML alike", () => {
    // The filename-earned "Page" split is dead; the label needs no filename at all.
    expect(artifactKindLabel("document")).toBe("Document");
    expect(artifactIcon("document")).toBe(FileText);
  });
  it("deck speaks as Presentation, webapp as App, collection as Collection", () => {
    expect(artifactKindLabel("deck")).toBe("Presentation");
    expect(artifactKindLabel("webapp")).toBe("App");
    expect(artifactKindLabel("collection")).toBe("Collection");
    expect(artifactKindLabel("spreadsheet")).toBe("Spreadsheet");
    expect(artifactKindLabel("code")).toBe("Code");
  });
  it("unknown kinds degrade to the safe words", () => {
    expect(artifactKindLabel("mystery")).toBe("Artifact");
    expect(artifactKindLabel(null)).toBe("Artifact");
    expect(artifactIcon("mystery")).toBe(File);
  });
  it("icons are per-kind, never per-filename", () => {
    expect(artifactIcon("deck")).toBe(Presentation);
    expect(artifactIcon("webapp")).toBe(AppWindow);
  });
});
