/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { EditableText } from "./document-edit/editable-text";
import type { RendererProps } from "./registry";

// Enough to see what the file is; a dataset is downloaded, not read in a drawer. The cut says so.
const TEXT_CAP = 100_000;

/** Plain text (`.txt`, `.csv`, `.jsonl` heads) - a quiet mono block, never mis-parsed as
 * markdown. The edit frame gates on the DOCUMENT kind inside, so a `.txt` document
 * grows Edit while a `.csv` dataset riding the same hint never does. */
export function TextView({ artifact, text, onArtifactChanged }: RendererProps) {
  const value = text ?? "";
  const shown = value.length > TEXT_CAP ? value.slice(0, TEXT_CAP) : value;
  return (
    <EditableText artifact={artifact} text={text} onArtifactChanged={onArtifactChanged}>
      <div className="ring-foreground/10 overflow-x-auto rounded-lg p-4 ring-1">
        <pre className="font-mono text-caption-md-regular leading-relaxed whitespace-pre-wrap break-words">
          {shown}
        </pre>
        {value.length > TEXT_CAP && (
          <p className="text-muted-foreground mt-2 text-caption-md-regular">
            Showing the first {TEXT_CAP.toLocaleString("en-US")} characters - download for the whole
            file.
          </p>
        )}
      </div>
    </EditableText>
  );
}
