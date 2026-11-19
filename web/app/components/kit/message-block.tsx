/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// ```message → the chat/DM draft card (ADR-053): the email card's sibling for Slack/SMS/DM
// drafts. Same header grammar (To or Channel, optional Subject), the body styled as one
// sent bubble so it reads as the message it will become. Same actions, same law: the fence
// presents; the user acts.
import { Badge } from "@makeplane/propel/components/badge";
import {
  draftClipboardPayload,
  escapeHtml,
  MESSAGE_KEYS,
  parseDraftHeaders,
  writeClipboard,
} from "./email-draft";
import { CopyAction, UseDraftAction } from "./draft-actions";
import { SpecialBlockShell, type SpecialBlockProps } from "./special-blocks";
import { MessageSquareIcon } from "lucide-react";
import { useRef } from "react";

export function MessageBlock({ raw, streaming, ordinal, total, renderBody }: SpecialBlockProps) {
  const { headers, body } = parseDraftHeaders(raw, MESSAGE_KEYS);
  const bodyRef = useRef<HTMLDivElement>(null);
  // || not ??: a present-but-empty "Channel:" line parses to "" and must fall through to
  // To, not blank the destination.
  const destination = headers.channel || headers.to;

  const copy = () => {
    const bodyHtml = bodyRef.current?.innerHTML ?? escapeHtml(body);
    return writeClipboard(
      draftClipboardPayload(
        [
          ["To", headers.to],
          ["Channel", headers.channel],
          ["Subject", headers.subject],
        ],
        body,
        bodyHtml,
      ),
    );
  };

  return (
    <SpecialBlockShell
      block="message"
      icon={MessageSquareIcon}
      title={`Message draft${destination ? ` · ${destination}` : ""}`}
      actions={
        total > 1 && (
          <Badge variant="neutral" size="xs" label={`Draft ${ordinal + 1} of ${total}`} />
        )
      }
    >
      <div className="px-3 py-3">
        <div ref={bodyRef} className="bg-muted rounded-lg px-3 py-2">
          {renderBody(body)}
        </div>
      </div>
      {!streaming && (
        <footer className="flex items-center justify-end gap-1 px-2 pb-2">
          <UseDraftAction draft={`Send this message as written:\n\n${raw}`} />
          <CopyAction onCopy={copy} label="Copy message" />
        </footer>
      )}
    </SpecialBlockShell>
  );
}
