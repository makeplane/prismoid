/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// ```email → the letter card (ADR-053). To/Cc/Bcc/Subject as labeled rows above the body
// (the Gemini/Copilot consensus), the body as rendered markdown, actions underneath once
// the fence has settled. Several fences in one answer wear "Draft n of N" so picking one
// of three is a glance, not a scroll-and-count. Copy is dual-MIME: the rich flavor pastes
// straight into a mail composer, the plain flavor is the draft verbatim.
import { Badge } from "@makeplane/propel/components/badge";
import { draftClipboardPayload, escapeHtml, parseEmailDraft, writeClipboard } from "./email-draft";
import { CopyAction, UseDraftAction } from "./draft-actions";
import { SpecialBlockShell, type SpecialBlockProps } from "./special-blocks";
import { MailIcon } from "lucide-react";
import { useRef } from "react";

const FIELD_LABELS = [
  ["To", "to"],
  ["Cc", "cc"],
  ["Bcc", "bcc"],
  ["Subject", "subject"],
] as const;

export function EmailBlock({ raw, streaming, ordinal, total, renderBody }: SpecialBlockProps) {
  const draft = parseEmailDraft(raw);
  const bodyRef = useRef<HTMLDivElement>(null);

  const copy = () => {
    const bodyHtml = bodyRef.current?.innerHTML ?? escapeHtml(draft.body);
    return writeClipboard(
      draftClipboardPayload(
        FIELD_LABELS.map(([label, key]) => [label, draft[key]] as const),
        draft.body,
        bodyHtml,
      ),
    );
  };

  const fields = FIELD_LABELS.filter(([, key]) => draft[key]);

  return (
    <SpecialBlockShell
      block="email"
      icon={MailIcon}
      title="Email draft"
      actions={
        total > 1 && (
          <Badge variant="neutral" size="xs" label={`Draft ${ordinal + 1} of ${total}`} />
        )
      }
    >
      {fields.length > 0 && (
        <div className="border-border grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 border-b px-3 py-2">
          {fields.map(([label, key]) => (
            <div key={key} className="contents">
              <span className="text-muted-foreground text-caption-md-regular leading-6">
                {label}
              </span>
              <span className="min-w-0 truncate text-body-sm-regular leading-6" title={draft[key]}>
                {draft[key]}
              </span>
            </div>
          ))}
        </div>
      )}
      <div ref={bodyRef} className="px-3 py-1">
        {renderBody(draft.body)}
      </div>
      {!streaming && (
        <footer className="flex items-center justify-end gap-1 px-2 pb-2">
          {/* The prefill carries the send intent (the plan's grammar): the user reviews it
              in the composer and the SEND is their turn - with a mail connector attached
              the agent executes it; without one it answers honestly about what it needs. */}
          <UseDraftAction draft={`Send this email as written:\n\n${raw}`} />
          <CopyAction onCopy={copy} label="Copy email" />
        </footer>
      )}
    </SpecialBlockShell>
  );
}
