/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The option BLOCK ("make every single block a nice little
// component") - one dropdown row as the Linear/Raycast menu grammar: an optional icon well,
// the label, and the hint STACKED beneath it instead of squeezed to the right. Lives in the
// KIT - the vendored `ui/select` stays a byte-honest shell; any dropdown whose rows deserve
// more than a word composes this inside `<SelectItem>`. Spans only: the content nests
// inside Base UI's ItemText (a span), where block elements would be invalid.
import type { ComponentType } from "react";

export function SelectOption({
  icon: Icon,
  label,
  hint,
}: {
  /** Optional leading glyph - rendered in a quiet square well (menu-scale, 26px). */
  icon?: ComponentType<{ className?: string }>;
  label: string;
  /** One quiet line under the label. Absent = a compact single-line row. */
  hint?: string;
}) {
  return (
    <span className="flex min-w-0 items-center gap-2.5 py-0.5">
      {Icon && (
        <span className="bg-muted text-muted-foreground flex size-6.5 shrink-0 items-center justify-center rounded-sm">
          <Icon className="size-3.5" />
        </span>
      )}
      <span className="flex min-w-0 flex-col text-left">
        <span className="text-caption-md-regular/5 font-medium">{label}</span>
        {hint && <span className="text-muted-foreground truncate text-[0.6875rem]/4">{hint}</span>}
      </span>
    </span>
  );
}
