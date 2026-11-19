/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { formatIcon, parseIcon } from "./value";
import { ProjectIcon } from "./project-icon";

describe("icon value vocabulary", () => {
  it("round-trips both kinds", () => {
    expect(parseIcon("lucide:rocket")).toEqual({ kind: "lucide", name: "rocket" });
    expect(parseIcon("emoji:🚀")).toEqual({ kind: "emoji", char: "🚀" });
    expect(formatIcon({ kind: "lucide", name: "rocket" })).toBe("lucide:rocket");
    expect(formatIcon({ kind: "emoji", char: "🚀" })).toBe("emoji:🚀");
  });

  it("treats junk as the default face, never a crash", () => {
    expect(parseIcon(null)).toBeNull();
    expect(parseIcon("")).toBeNull();
    expect(parseIcon("svg:<script>")).toBeNull();
  });
});

describe("ProjectIcon", () => {
  it("renders an emoji as itself, inside a hue-tinted well", () => {
    const html = renderToStaticMarkup(<ProjectIcon icon="emoji:🚀" hue={3} />);
    expect(html).toContain("🚀");
    expect(html).toContain("bg-hue-4/10"); // hue index 3 → the 4th ramp entry
  });

  it("falls back to the default folder for unknown slugs and neutral for no hue", () => {
    const html = renderToStaticMarkup(<ProjectIcon icon="lucide:not-a-real-slug" hue={null} />);
    expect(html).toContain("svg"); // the FolderKanban fallback glyph rendered
    expect(html).toContain("bg-muted");
    expect(html).not.toContain("bg-hue");
  });
});
