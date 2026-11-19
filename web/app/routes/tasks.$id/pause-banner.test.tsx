/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/** The paused line (ADR-064), and a step waiting for its device. */
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { PauseBanner } from "./pause-banner";

describe("PauseBanner", () => {
  it("PauseBanner reads Waiting for MacBook Pro", () => {
    const html = renderToStaticMarkup(
      <PauseBanner pause={{ by: "", applied: true }} away="Waiting for MacBook Pro" />,
    );
    expect(html).toContain("Waiting for MacBook Pro");
    expect(html).not.toContain("Paused");
  });

  it("a person's pause still reads as theirs", () => {
    const html = renderToStaticMarkup(
      <PauseBanner pause={{ by: "Ada", applied: true }} away={null} />,
    );
    expect(html).toContain("Paused by Ada");
  });
});
