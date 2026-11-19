/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The end-of-response row (ADR-019): the quiet affordances that say "the answer is done and
// it's yours" - Copy, the thumbs, and Retry. Renders only on a settled task, after the last
// feed item, in the reading column. Route-local by the layers law; composed from kit/ui
// primitives only (CopyButton is the kit's; the thumbs follow the icon-button convention).

import { RotateCcw, ThumbsDown, ThumbsUp } from "lucide-react";
import { CopyButton } from "~/components/kit";
import { Button } from "@makeplane/propel/components/button";
import { Icon } from "@makeplane/propel/components/icon";
import { IconButton } from "@makeplane/propel/components/icon-button";

export type Rating = "up" | "down";

export function ResponseActions({
  answerText,
  feedback,
  onFeedback,
  onRetry,
  retrying,
}: {
  /** What Copy puts on the clipboard - the delivered summary, else the last answer segment. */
  answerText: string;
  feedback: Rating | null;
  /** Absent = the caller may not write to this task (a Project viewer: viewers
   *  watch): a grade is a durable `task.feedback` write the server refuses them. */
  onFeedback?: (rating: Rating) => void;
  /** Absent = the caller may not write to this task (a Project viewer): Retry is not offered,
   *  because the server would refuse it. Copy is a read, and stays. */
  onRetry?: () => void;
  /** The in-flight latch: a lifecycle guard (the Archive/Delete precedent), not a capability. */
  retrying: boolean;
}) {
  return (
    <div
      role="group"
      aria-label="Response actions"
      className="text-muted-foreground -ml-1.5 flex items-center gap-1"
    >
      <CopyButton value={answerText} />
      {onFeedback && (
        <>
          <IconButton
            variant={feedback === "up" ? "tertiary" : "ghost"}
            size="md"
            icon={<Icon icon={ThumbsUp} />}
            aria-pressed={feedback === "up"}
            aria-label="Good response"
            onClick={() => onFeedback("up")}
          />
          <IconButton
            variant={feedback === "down" ? "tertiary" : "ghost"}
            size="md"
            icon={<Icon icon={ThumbsDown} />}
            aria-pressed={feedback === "down"}
            aria-label="Bad response"
            onClick={() => onFeedback("down")}
          />
        </>
      )}
      {onRetry && (
        <Button
          variant="ghost"
          size="sm"
          stretch="auto"
          icon={<Icon icon={RotateCcw} />}
          label="Retry"
          onClick={onRetry}
          disabled={retrying}
          title="Run the latest ask again as a new turn"
        />
      )}
    </div>
  );
}
