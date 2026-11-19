/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The special-block registry (ADR-053): fence language → rendered card. One entry per block,
// one file per block - the artifact-renderer grammar. The LAW (held here):
// a fence is PRESENTATION only. A block renders model-authored content; its actions are user
// gestures (copy, a composer prefill the user reviews and sends); nothing a fence carries
// ever executes, fetches, or sends on its own.
import type { ComponentType, ReactNode } from "react";
import { createContext, useContext } from "react";
import { EmailBlock } from "./email-block";
import { MessageBlock } from "./message-block";
import { CsvBlock } from "./csv-block";
import { MermaidBlock } from "./mermaid-block";

export interface SpecialBlockProps {
  /** The fence's raw source, verbatim. */
  raw: string;
  /** True while the fence is still open at the live stream's tail - blocks render
   *  progressively but hide their actions (half a draft must not be copyable as final). */
  streaming: boolean;
  /** 0-based position among this source's fences of the SAME language, and their count -
   *  "Draft 2 of 3" grammar. Computed by a pure pre-scan, never a render-order counter
   *  (memoized blocks skip renders; a counter would drift). */
  ordinal: number;
  total: number;
  /** Renders markdown WITHOUT special-block dispatch (fences inside a body stay plain code
   *  blocks) - injected by markdown.tsx so a block never imports the renderer back. */
  renderBody: (md: string) => ReactNode;
}

export type SpecialBlock = ComponentType<SpecialBlockProps>;

/** Fence language → block. Adding a block = one component file + one row here. */
export const SPECIAL_BLOCKS: Record<string, SpecialBlock> = {
  email: EmailBlock,
  message: MessageBlock,
  csv: CsvBlock,
  mermaid: MermaidBlock,
};

/** The pasted-card shell every block wears: card ring, h-9 header with an icon + title on
 *  the left and actions on the right, the body below. One definition (the four blocks had
 *  drifted the same class strings byte-for-byte); the title always truncates (min-w-0 +
 *  truncate - a long "Message draft · #channel" must clip, not widen the card). `meta` is
 *  the quiet annotation riding inside the title span (row counts, failure notes). */
export function SpecialBlockShell({
  block,
  icon: Icon,
  title,
  meta,
  actions,
  children,
}: {
  /** data-block value - the block's name, pinned by tests. */
  block: string;
  icon: ComponentType<{ className?: string }>;
  title: ReactNode;
  meta?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section
      data-block={block}
      className="bg-card ring-foreground/10 my-3 overflow-hidden rounded-lg shadow-raised-100 ring-1"
    >
      <header className="border-border bg-muted/50 flex h-9 items-center justify-between border-b px-3">
        <span className="text-muted-foreground flex min-w-0 items-center gap-1.5 text-caption-md-regular font-medium">
          <Icon className="size-3.5 shrink-0" />
          <span className="truncate">{title}</span>
          {meta}
        </span>
        {actions}
      </header>
      {children}
    </section>
  );
}

/** Actions a HOST surface can honor, offered to blocks via context. Absent context (or an
 *  absent handler) means the affordance is ABSENT - never disabled with an excuse
 *  (DESIGN.md · Capability). The task page provides `onUseDraft` (composer prefill); a
 *  send-capable handler arrives with mail connectors and upgrades the same spot. */
export interface MarkdownActions {
  onUseDraft?: (draft: string) => void;
}

const ActionsContext = createContext<MarkdownActions>({});

export const MarkdownActionsProvider = ActionsContext.Provider;

export function useMarkdownActions(): MarkdownActions {
  return useContext(ActionsContext);
}
