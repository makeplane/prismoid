/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { type ReactNode } from "react";
import { type Tone } from "~/components/kit";

export const TONES: Tone[] = ["muted", "info", "success", "warning", "danger"];

/** One labeled display row - the gallery's grid unit. `file` names the home in the codebase. */
export function Specimen({
  label,
  file,
  note,
  children,
}: {
  label: string;
  file?: string;
  note?: string;
  children: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-start gap-3">
      <div className="w-40 shrink-0 pt-0.5">
        <span className="text-caption-md-regular">{label}</span>
        {file && (
          <code
            title={file}
            className="text-muted-foreground/60 block truncate font-mono text-caption-md-regular"
          >
            {file}
          </code>
        )}
      </div>
      <div className="flex min-w-0 flex-1 flex-wrap items-center gap-2">
        {children}
        {note && (
          <span className="text-muted-foreground/70 basis-full text-caption-md-regular">
            {note}
          </span>
        )}
      </div>
    </div>
  );
}

// ── The radius lab ───────────────────────────────────────────────────────────
