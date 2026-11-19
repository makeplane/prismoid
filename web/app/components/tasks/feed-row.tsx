/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// One feed item, in the thread's slim voice - machinery stays quiet; prose and deliverables
// carry the thread (the same hierarchy the task page draws, at DM scale). Born in the bots
// messenger (bot-chat), shared with its task peek, promoted here on its third consumer: the
// admin console's task peek folds the same durable record through the same reducer and must
// read identically. `onOpenArtifact` is optional - a host with no reader (the admin peek
// reads another user's task; it can't presign their artifacts) renders the card as a quiet
// fact instead of a dead button (the capability law).

import { Markdown } from "~/components/kit";
import { formatDurationMs, formatSteps, formatTokens } from "~/lib/time";
import type { FeedItem } from "~/lib/task-view";

export function FeedRow({
  item,
  onOpenArtifact,
}: {
  item: FeedItem;
  onOpenArtifact?: (id: string) => void;
}) {
  switch (item.kind) {
    case "user":
      return (
        <div className="flex justify-end">
          <div className="bg-secondary text-secondary-foreground max-w-[72%] rounded-lg px-3.5 py-2.5 text-body-sm-regular leading-relaxed whitespace-pre-wrap">
            {item.text}
          </div>
        </div>
      );
    case "working-artifacts":
      // The chip row, peek-scale: quiet facts (no reader in the admin peek - the
      // capability law keeps them plain there).
      return (
        <div className="flex flex-wrap items-center gap-1.5">
          {item.items.map((c) =>
            onOpenArtifact ? (
              <button
                key={c.artifactId}
                type="button"
                onClick={() => onOpenArtifact(c.artifactId)}
                className="ring-foreground/10 hover:ring-foreground/25 bg-card flex max-w-56 items-center gap-1.5 rounded-md px-2 py-1 text-left text-caption-md-regular ring-1 transition-shadow"
                title={c.title}
              >
                <span className="min-w-0 truncate">{c.title}</span>
              </button>
            ) : (
              <span
                key={c.artifactId}
                className="ring-foreground/10 bg-card flex max-w-56 items-center gap-1.5 rounded-md px-2 py-1 text-caption-md-regular ring-1"
                title={c.title}
              >
                <span className="min-w-0 truncate">{c.title}</span>
              </span>
            ),
          )}
        </div>
      );
    case "segment":
      // Parity with the task page's segment render: live streaming variants for
      // open fences, and an empty live segment shimmers instead of rendering a blank row.
      // (`prose-sm` was a dead class here - no typography plugin exists, by decree.)
      return (
        <div className="max-w-[86%]">
          {item.markdown || !item.live ? (
            <Markdown live={item.live === true} breaks>
              {item.markdown}
            </Markdown>
          ) : (
            <span className="text-muted-foreground shimmer">…</span>
          )}
        </div>
      );
    case "artifact":
      if (!onOpenArtifact) {
        return (
          <div className="ring-foreground/10 bg-card flex w-fit max-w-md items-center gap-2.5 rounded-md px-3.5 py-2.5 ring-1">
            <span className="min-w-0 truncate text-body-sm-regular font-medium">
              {item.title ?? "Deliverable"}
            </span>
            <span className="text-muted-foreground shrink-0 text-caption-md-regular">
              published
            </span>
          </div>
        );
      }
      return (
        <button
          type="button"
          onClick={() => onOpenArtifact(item.artifactId)}
          className="ring-foreground/10 hover:ring-foreground/25 bg-card flex w-fit max-w-md items-center gap-2.5 rounded-md px-3.5 py-2.5 text-left ring-1 transition-shadow"
        >
          <span className="min-w-0 truncate text-body-sm-regular font-medium">
            {item.title ?? "Deliverable"}
          </span>
          <span className="text-muted-foreground shrink-0 text-caption-md-regular">open →</span>
        </button>
      );
    case "qa":
      return (
        <div className="text-muted-foreground space-y-1 text-caption-md-regular">
          {item.items.map((qa, i) => (
            <p key={i}>
              <span className="text-foreground font-medium">{qa.prompt}</span> · {qa.answer}
            </p>
          ))}
        </div>
      );
    case "escalation":
      return (
        <div className="border-destructive/25 bg-destructive/10 max-w-[86%] rounded-md border px-3.5 py-2.5 text-body-sm-regular">
          <p className="font-medium">{item.headline}</p>
          <p className="text-muted-foreground pt-1 text-caption-md-regular">{item.reason}</p>
        </div>
      );
    case "epilogue": {
      // The turn's conclusion rides the delivered line here too (round seven) - the
      // peek folds the same record, so it tells the same total.
      const r = item.receipt;
      const bits = r
        ? [
            formatSteps(r.steps),
            r.durationMs != null ? formatDurationMs(r.durationMs) : null,
            r.tokens != null || r.cachedTokens != null
              ? formatTokens(r.tokens ?? 0, r.cachedTokens)
              : null,
          ].filter(Boolean)
        : [];
      return (
        <p className="text-muted-foreground text-caption-md-regular">
          {item.delivered ? "Delivered" : "Settled"}
          {bits.map((b) => ` · ${b}`).join("")}
          {item.verdict ? ` · verification ${item.verdict}` : ""}
        </p>
      );
    }
    case "execution":
      return item.settled ? (
        <p className="text-muted-foreground text-caption-md-regular">
          Completed {item.groups.length} step{item.groups.length === 1 ? "" : "s"}
        </p>
      ) : null; // the host's working line carries liveness
    case "marker":
      return <p className="text-muted-foreground text-caption-md-regular italic">{item.text}</p>;
    default:
      return null;
  }
}
