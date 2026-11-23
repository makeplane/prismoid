/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from "vitest";
import { listOwnsKeys, ROW_ATTR } from "./keys";

afterEach(() => {
  document.body.innerHTML = "";
});

function el(html: string): Element {
  document.body.innerHTML = html;
  return document.querySelector("[data-target]") ?? document.body;
}

describe("listOwnsKeys", () => {
  it("the page itself, and anything with no keys of its own, leaves them to the list", () => {
    expect(listOwnsKeys(document.body)).toBe(true);
    expect(listOwnsKeys(el("<main><p data-target>hi</p></main>"))).toBe(true);
    expect(listOwnsKeys(null)).toBe(true);
  });

  it("a notification row leaves them to the list", () => {
    expect(listOwnsKeys(el(`<button ${ROW_ATTR}><span data-target>row</span></button>`))).toBe(
      true,
    );
  });

  it("a control with keys of its own keeps them", () => {
    expect(listOwnsKeys(el('<input data-target aria-label="x" />'))).toBe(false);
    expect(listOwnsKeys(el("<button data-target>Filter by kind</button>"))).toBe(false);
    expect(listOwnsKeys(el('<div role="menuitemradio" data-target>All</div>'))).toBe(false);
    expect(listOwnsKeys(el('<div role="separator" tabindex="0" data-target></div>'))).toBe(false);
    expect(listOwnsKeys(el('<div contenteditable="true" data-target></div>'))).toBe(false);
  });
});
