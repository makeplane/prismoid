/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { describe, expect, it } from "vitest";
import { deviceLine, expiryLabel } from "./access-copy";

describe("deviceLine", () => {
  it("names a browser and its platform", () => {
    expect(
      deviceLine(
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
      ),
    ).toBe("Chrome on macOS");
    expect(
      deviceLine(
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Safari/604.1",
      ),
    ).toBe("Safari on iPhone");
  });

  it("resolves the families that all claim to be each other", () => {
    // Edge says Chrome says Safari says Mozilla - most-specific first is the whole trick.
    expect(deviceLine("Mozilla/5.0 (Windows NT 10.0) Chrome/141 Edg/141")).toBe("Edge on Windows");
    expect(deviceLine("Mozilla/5.0 (X11; Linux x86_64) Firefox/130.0")).toBe("Firefox on Linux");
  });

  it("tells our own clients apart from a browser - the operator's actual question", () => {
    expect(deviceLine("prismoid-cli/0.1.0 (Darwin)")).toBe("Prismoid CLI");
    expect(deviceLine("Prismoid/1.2 Electron/33 (Mac OS X)")).toBe("Prismoid desktop app on macOS");
    expect(deviceLine("curl/8.7.1")).toBe("Script or tool");
  });

  it("passes an unrecognised agent through rather than guessing", () => {
    expect(deviceLine("something-nobody-has-seen/2")).toBe("something-nobody-has-seen/2");
  });

  it("names the absence honestly", () => {
    expect(deviceLine("")).toBe("Unknown device");
    expect(deviceLine("   ")).toBe("Unknown device");
  });
});

describe("expiryLabel", () => {
  const now = Date.parse("2026-08-23T12:00:00Z");
  it("speaks the future while the session lives", () => {
    expect(expiryLabel("2026-08-29T12:00:00Z", now)).toBe("expires in 6d");
    expect(expiryLabel("2026-08-23T12:30:00Z", now)).toBe("expires in 30m");
  });
  it("speaks the past once it has run out", () => {
    expect(expiryLabel("2026-08-23T09:00:00Z", now)).toBe("expired 3h ago");
    expect(expiryLabel("2026-08-23T12:00:00Z", now)).toBe("expired 0s ago");
  });
});
