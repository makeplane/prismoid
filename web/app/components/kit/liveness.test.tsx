/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The one liveness mark's contract, round two (owner: fade trio): three
// dots rippling by opacity alone with a clockwork stagger, and a reduced-motion fallback
// that stays VISIBLE - animate-none alone would freeze the ripple at 0.2 opacity and the
// mark would read as absence.
import { Liveness } from "./liveness";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

describe("Liveness", () => {
  it("renders three rippling dots with the stagger on their own delays", () => {
    const html = renderToStaticMarkup(<Liveness />);
    expect(html.match(/animate-liveness/g)).toHaveLength(3);
    expect(html).toContain("animation-delay:0ms");
    expect(html).toContain("animation-delay:170ms");
    expect(html).toContain("animation-delay:340ms");
    expect(html).toContain("rounded-full"); // true circles - the sanctioned use
  });

  it("falls still but VISIBLE under reduced motion", () => {
    const html = renderToStaticMarkup(<Liveness />);
    expect(html).toContain("motion-reduce:animate-none");
    expect(html).toContain("motion-reduce:opacity-60");
  });

  it("is decorative by default; announces itself only when asked to stand alone", () => {
    expect(renderToStaticMarkup(<Liveness />)).toContain('aria-hidden="true"');
    const alone = renderToStaticMarkup(<Liveness decorative={false} />);
    expect(alone).toContain('role="status"');
    expect(alone).toContain('aria-label="Working"');
  });
});
