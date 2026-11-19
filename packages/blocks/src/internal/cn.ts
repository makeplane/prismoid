/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { clsx, type ClassValue } from "clsx";
import { extendTailwindMerge } from "tailwind-merge";

// Propel's semantic type scale (text-body-sm-regular, text-h5-medium, ...) sets a font size;
// stock tailwind-merge reads any unknown text-* as a color and drops one of
// `text-body-sm-regular text-secondary`. Same for Propel's named border widths (border-sm).
const isPropelType = (value: string) =>
  /^(h[1-6]|body-(2xs|xs|sm|md)|caption-(3xs|2xs|xs|sm|md))-(regular|medium|semibold|bold)$/.test(
    value,
  );
const WIDTHS = ["xs", "sm", "md", "lg", "xl"];

const twMerge = extendTailwindMerge({
  extend: {
    classGroups: {
      "font-size": [{ text: [isPropelType] }],
      "border-w": [{ border: WIDTHS }],
      "border-w-t": [{ "border-t": WIDTHS }],
      "border-w-r": [{ "border-r": WIDTHS }],
      "border-w-b": [{ "border-b": WIDTHS }],
      "border-w-l": [{ "border-l": WIDTHS }],
    },
  },
});

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
