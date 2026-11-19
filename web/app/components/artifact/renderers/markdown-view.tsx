/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { Markdown } from "~/components/kit";
import { EditableText } from "./document-edit/editable-text";
import type { RendererProps } from "./registry";

/** The `.md` renderer - the extracted body of the old `Preview`, reachable only via the
 * server's `markdown` hint now, so it can never again be handed a binary. Wrapped in the
 * edit frame: a markdown DOCUMENT grows an Edit affordance; markdown that is not
 * a document (nothing today) would not. */
export function MarkdownView({ artifact, text, onArtifactChanged }: RendererProps) {
  return (
    <EditableText artifact={artifact} text={text} onArtifactChanged={onArtifactChanged}>
      <div className="ring-foreground/10 overflow-hidden rounded-lg p-4 ring-1">
        {/* Prose owns its measure wherever the page is wide - a memo is a column, not a banner. */}
        <div className="max-w-3xl">
          <Markdown>{text ?? ""}</Markdown>
        </div>
      </div>
    </EditableText>
  );
}
