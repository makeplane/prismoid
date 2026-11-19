/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The draft blocks' shared action row (ADR-053): copy with the check latch, and
// "Use this draft" when a host provides the composer handle. Absent handler = absent
// button (DESIGN.md · Capability) - never a disabled button with an excuse. Actions are
// user gestures on model-authored CONTENT; nothing here executes or sends.
import { Button } from "@makeplane/propel/components/button";
import { Icon } from "@makeplane/propel/components/icon";
import { CopyButton } from "./primitives";
import { useMarkdownActions } from "./special-blocks";
import { PenLineIcon } from "lucide-react";

/** The kit's one copy control, worn by the blocks. Kept as a name (not a bare re-export)
 *  so the block files read as gestures, and `onCopy` stays required here - a block always
 *  writes its own payload (dual-MIME where it can offer it; throwing skips the latch). */
export function CopyAction({
  onCopy,
  label,
}: {
  onCopy: () => Promise<unknown> | unknown;
  label?: string;
}) {
  return <CopyButton onCopy={onCopy} label={label} />;
}

export function UseDraftAction({ draft }: { draft: string }) {
  const { onUseDraft } = useMarkdownActions();
  if (!onUseDraft) return null;
  return (
    <Button
      variant="ghost"
      size="md"
      stretch="auto"
      icon={<Icon icon={PenLineIcon} />}
      label="Use this draft"
      onClick={() => onUseDraft(draft)}
    />
  );
}
