/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { AllowedSitesList } from "./route";

describe("AllowedSitesList", () => {
  it("renders hosts with their provenance and a revoke control", () => {
    const html = renderToStaticMarkup(
      <AllowedSitesList
        rows={[
          {
            id: "1",
            host: "admin.example.com",
            source: "gate",
            gate_id: "g",
            created_at: "2026-09-28T10:00:00Z",
            revoked_at: null,
          },
          {
            id: "2",
            host: "docs.vendor.com",
            source: "settings",
            gate_id: null,
            created_at: "2026-09-28T11:00:00Z",
            revoked_at: null,
          },
        ]}
        onRevoke={() => {}}
      />,
    );
    expect(html).toContain("admin.example.com");
    expect(html).toContain("from a task");
    expect(html).toContain("added here");
    expect(html.match(/Revoke/g)?.length).toBe(2);
  });

  it("says what the empty state means", () => {
    const html = renderToStaticMarkup(<AllowedSitesList rows={[]} onRevoke={() => {}} />);
    expect(html).toContain("No always-allowed sites");
  });
});
