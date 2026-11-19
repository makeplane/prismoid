/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { expect, test } from "vitest";

import { cn } from "./cn";

test("Propel's type scale is a font size, so it survives a text color", () => {
  expect(cn("text-body-sm-regular text-secondary")).toBe("text-body-sm-regular text-secondary");
  expect(cn("text-h5-medium text-primary")).toBe("text-h5-medium text-primary");
});

test("a later font size replaces Propel's, and the reverse", () => {
  expect(cn("text-body-sm-regular", "text-xs")).toBe("text-xs");
  expect(cn("text-sm", "text-caption-md-medium")).toBe("text-caption-md-medium");
});

test("Propel's named border width survives a border color", () => {
  expect(cn("border-sm border-subtle")).toBe("border-sm border-subtle");
  expect(cn("border-b-sm border-strong")).toBe("border-b-sm border-strong");
});
