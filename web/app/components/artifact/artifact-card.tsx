/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { artifactIcon, artifactKindLabel, IconTile, VerdictBadge } from "~/components/kit";
import type { ArtifactView } from "~/lib/task-view";
import { cn } from "~/lib/utils";

/** The deliverable as a row you can open - one card for every surface that lists what a task
 *  made (the transcript, the inbox). Promoted from the task page's FeedRow on its second
 *  consumer.
 *  `superseded`: the line saying a steer's re-run replaced it ("Superseded by Bob's steer").
 *  It is still openable as history, but greyed and labelled, never presented as the
 *  delivered card.
 *  ring-inset: an outset ring is a box-shadow floating 1px OUTSIDE the fill - zoomed on a
 *  dark canvas it reads as a detached rim with a doubled corner arc. Inset shares the
 *  fill's geometry, so the edge is one clean curve. */
export function ArtifactCard({
  title,
  artifact,
  onOpen,
  superseded,
}: {
  title?: string | null;
  artifact?: ArtifactView;
  onOpen: () => void;
  superseded?: string;
}) {
  return (
    <button
      onClick={onOpen}
      className={cn(
        "ring-foreground/10 hover:ring-foreground/25 bg-card flex w-full items-center gap-3 rounded-lg p-3 text-left ring-1 ring-inset transition-shadow",
        superseded && "opacity-60",
      )}
    >
      <IconTile icon={artifactIcon(artifact?.type)} />
      <div className="min-w-0 flex-1">
        <p className="truncate text-body-sm-regular font-medium">{title ?? "Artifact"}</p>
        <p className="text-muted-foreground text-caption-md-regular">
          {superseded ?? artifactKindLabel(artifact?.type)}
        </p>
      </div>
      {!superseded && <VerdictBadge verdict={artifact?.verdict} showIcon={false} />}
    </button>
  );
}
