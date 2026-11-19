/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/** The deliverable being composed, LIVE - the streaming answer the transcript used to
 *  hide while it was born inside a tool call's arguments. Grows via artifact.delta
 *  frames and retires when artifact.produced hands over to the real card.
 *
 *  Two faces, one chrome:
 *  - prose (`draft.md`) pours through Markdown - the document composes in front of you;
 *  - a structured `.json` draft (a collection) is NOT poured through Markdown - raw JSON
 *    is not a preview, it is noise - it gets the live BREAKDOWN instead (draft-shape.ts
 *    decides, this file renders): the title as it lands, the columns one by one, the row
 *    counter climbing with the newest row named, the views last. "Building - 23 rows"
 *    alone answered nothing ("I have NO idea what is happening").
 */

import { FileText } from "lucide-react";
import { Liveness } from "~/components/kit";
import { useMemo } from "react";
import { Markdown } from "~/components/kit";
import { useSmoothText } from "~/lib/smooth-text";
import { cn } from "~/lib/utils";
import { collectionDraftShape } from "./draft-shape";

export function DraftCard({ path, text }: { path: string; text: string }) {
  if (path.endsWith(".json")) {
    return <StructuredDraftCard path={path} text={text} />;
  }
  return <ProseDraftCard path={path} text={text} />;
}

/** Exported for the breakdown render pin in route.test.tsx. */
export function StructuredDraftCard({ path, text }: { path: string; text: string }) {
  // Recomputed per ~256 B of growth - roughly once per streamed row, so the counter and
  // the row tail move at the rhythm the rows actually land, and never a JSON.parse (the
  // body is incomplete by definition; see draft-shape.ts). `path` guards the draft-swap
  // edge: a NEW draft landing in the same bucket must not show the old draft's shape.
  const bucket = text.length >> 8;
  const shape = useMemo(
    () => collectionDraftShape(text),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the bucket IS the text dependency
    [path, bucket],
  );
  // The organizer names its own file in the stream's first key - shed the scratch-path
  // "draft" name the moment the real one lands.
  const name = shape.filename
    ? `${shape.filename}.collection.json`
    : path.split("/").pop() || "draft";
  const hasBody =
    shape.title != null ||
    shape.summary != null ||
    shape.fields.length > 0 ||
    shape.rows > 0 ||
    shape.views.length > 0;
  return (
    <div className="ring-foreground/10 bg-card overflow-hidden rounded-lg ring-1 ring-inset">
      <div className="text-muted-foreground flex items-center gap-2 px-3 py-2 text-caption-md-regular">
        <FileText className="size-3.5 shrink-0" />
        <span className="min-w-0 truncate font-medium">{name}</span>
        <Liveness className="ml-auto size-3" />
        <span className="shrink-0">
          building
          {shape.rows > 0 ? ` - ${shape.rows} row${shape.rows === 1 ? "" : "s"} so far` : "…"}
        </span>
      </div>
      {hasBody && (
        <div className="text-muted-foreground space-y-1 border-t px-4 py-3 text-caption-md-regular">
          {shape.title && (
            <p className="text-foreground truncate text-body-sm-regular font-medium">
              {shape.title}
            </p>
          )}
          {shape.summary && (
            <p className="text-muted-foreground/80 line-clamp-2">{shape.summary}</p>
          )}
          {shape.fields.length > 0 && (
            <p className="truncate" title={shape.fields.join(" · ")}>
              {shape.fields.length} column
              {shape.fields.length === 1 ? "" : "s"} - {shape.fields.join(" · ")}
            </p>
          )}
          {/* The heartbeat: the table's visible tail - the last few rows, newest arrowed. */}
          {shape.recent.length > 0 && (
            <div className="space-y-0.5 pt-0.5">
              {shape.recent.map((row, i) => {
                const newest = i === shape.recent.length - 1;
                return (
                  <p
                    key={`${i}-${row}`}
                    className={cn(
                      "truncate",
                      newest ? "text-muted-foreground/80" : "text-muted-foreground/50",
                    )}
                  >
                    {newest ? `→ ${row}` : row}
                  </p>
                );
              })}
            </div>
          )}
          {shape.views.length > 0 && (
            <p className="text-muted-foreground/70 truncate">
              Views: {shape.views.map(cap).join(" · ")}
            </p>
          )}
        </div>
      )}
    </div>
  );
}

function cap(s: string): string {
  return s ? s[0].toUpperCase() + s.slice(1) : s;
}

function ProseDraftCard({ path, text }: { path: string; text: string }) {
  const smooth = useSmoothText(text, true);
  const name = path.split("/").pop() || "draft";
  return (
    <div className="ring-foreground/10 bg-card overflow-hidden rounded-lg ring-1 ring-inset">
      <div className="text-muted-foreground flex items-center gap-2 border-b px-3 py-2 text-caption-md-regular">
        <FileText className="size-3.5 shrink-0" />
        <span className="min-w-0 truncate font-medium">{name}</span>
        <Liveness className="ml-auto size-3" />
        <span className="shrink-0">drafting…</span>
      </div>
      <div className="stream-tail px-4 py-3 text-body-sm-regular">
        <Markdown live>{smooth}</Markdown>
      </div>
    </div>
  );
}
