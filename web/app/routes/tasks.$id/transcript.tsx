/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { Fragment, type RefObject } from "react";
import { Link } from "react-router";
import {
  LabeledDivider,
  LoadingState,
  MarkdownActionsProvider,
  type MarkdownActions,
} from "~/components/kit";
import {
  MessageScroller,
  MessageScrollerButton,
  MessageScrollerContent,
  MessageScrollerItem,
  MessageScrollerProvider,
  MessageScrollerViewport,
} from "@prismoid/blocks/message-scroller";
import { taskHref } from "~/lib/links";
import type { ArtifactView, FeedItem, TaskView } from "~/lib/task-view";
import { cn } from "~/lib/utils";
import { DraftCard } from "./draft-card";
import { FeedRow } from "./feed-row";
import { PauseBanner } from "./pause-banner";
import type { PauseView } from "./progress-copy";
import { QueuedNotice } from "./queued-notice";
import { ResponseActions, type Rating } from "./response-actions";
import { ScrollBridge, type ScrollApi } from "./scroll-bridge";
import { ExecTail } from "./tail-line";
import type { Ancestor } from "./thread";
import { liveExecOf, liveNoticeOf } from "./task-page";
import { WorkingLine } from "./working-line";

// The reading column: every turn, marker, card, and the composer share one measure (~68ch) -
// the transcript reads like a document, not a full-bleed dashboard. CENTERED, the way every
// vendor centers a conversation: with the rail closed it centers in the window; with the rail
// open it centers in the remaining space - the flex row handles both, no conditional.
export const COL = "mx-auto w-full max-w-3xl";

type FeedProps = {
  artifacts: ArtifactView[];
  onOpenArtifact: (id: string) => void;
  /** A shared task: turns name their authors and control lines are drawn. */
  shared: boolean;
};

// Control lines ("Ada paused") are the record of a SHARED task. On a personal task the only
// person acting is you, and the line would only ever repeat what you just did.
const drawnOn = (shared: boolean) => (i: FeedItem) => shared || i.kind !== "control";

/** Prior turns from the conversation this task follows up on, then the fork's divider. */
function AncestorFeed({
  ancestors,
  ancestorsError,
  ...feed
}: FeedProps & { ancestors: Ancestor[]; ancestorsError: boolean }) {
  const drawn = drawnOn(feed.shared);
  const parent = ancestors.at(-1);
  return (
    <>
      {ancestorsError && (
        <p className={cn(COL, "text-muted-foreground text-caption-md-regular")}>
          Earlier turns in this thread couldn't be loaded.
        </p>
      )}
      {ancestors.map((a) => (
        <Fragment key={a.taskId}>
          {a.feed.filter(drawn).map((item) => (
            <MessageScrollerItem
              key={`${a.taskId}-${item.id}`}
              messageId={`${a.taskId}-${item.id}`}
              className={cn(COL, item.kind === "user" && "mt-6 first:mt-0")}
            >
              <FeedRow
                item={item}
                artifacts={feed.artifacts}
                onOpenArtifact={feed.onOpenArtifact}
                showAuthor={feed.shared}
              />
            </MessageScrollerItem>
          ))}
        </Fragment>
      ))}
      {parent && (
        <LabeledDivider className={COL}>
          <span className="min-w-0 max-w-[70%] truncate">
            forked from{" "}
            <Link to={taskHref({ id: parent.taskId })} className="underline underline-offset-2">
              {parent.goal || "the thread above"}
            </Link>
          </span>
        </LabeledDivider>
      )}
    </>
  );
}

export type TaskFeedProps = FeedProps & {
  view: TaskView;
  taskInputs?: { filename: string; bytes: number; media_type: string; url: string }[];
  questionOpen: boolean;
  myId?: string;
  /** Editor writes: absent for a viewer, who is shown the card, not the button. */
  onStartProposal?: (goal: string, deliverable: string | null) => Promise<void>;
  onRetract?: (commandId: string) => Promise<void> | void;
  onDeclineProposal: (id: string) => void;
};

/** This task's own turns. Each row receives only what its kind reads, so the rest of the
 *  feed keeps its memo on a streamed frame. */
function TaskFeed({ view, ...p }: TaskFeedProps) {
  const drawn = drawnOn(p.shared);
  return view.feed.filter(drawn).map((item) => {
    const steer = item.kind === "user" && item.steered;
    const shelf = item.kind === "artifact" || item.kind === "working-artifacts";
    return (
      <MessageScrollerItem
        key={item.id}
        messageId={item.id}
        className={cn(COL, item.kind === "user" && "mt-6 first:mt-0")}
      >
        <FeedRow
          item={item}
          artifacts={p.artifacts}
          onOpenArtifact={p.onOpenArtifact}
          onStartProposal={p.onStartProposal}
          onDeclineProposal={p.onDeclineProposal}
          showAuthor={p.shared}
          taskInputs={p.taskInputs}
          taskStatus={item.kind === "execution" ? view.status : undefined}
          // a steer's line waits on a pause or a question - steer rows only
          taskPaused={steer ? view.status === "paused" : undefined}
          questionOpen={steer ? p.questionOpen : undefined}
          // the author retracts an unapplied steer; a viewer writes nothing
          onRetract={p.onRetract}
          myId={p.myId}
          superseded={shelf ? view.superseded : undefined}
        />
      </MessageScrollerItem>
    );
  });
}

export type TailProps = {
  view: TaskView;
  live: boolean;
  gated: boolean;
  held: boolean;
  createdAt?: string;
  liveNote: string | null;
  pause: PauseView | null;
  answerText: string;
  retrying: boolean;
  /** Editor writes: grading and Retry (a viewer watches; Copy stays, it is a read). */
  onFeedback?: (rating: Rating) => void;
  onRetry?: () => void;
};

/** What stands under the transcript while the task is live and no gate is open. */
function LiveNoticeRow({ view, held, createdAt, liveNote, pause }: TailProps) {
  const notice = liveNoticeOf(view, held);
  if (notice === "held")
    // A held fork is PARKED on purpose - name the state ("Held"), say nothing runs, and say what
    // starts it: never "queued", never a Stop.
    return (
      <p className={cn(COL, "text-muted-foreground text-body-sm-regular")} role="status">
        <span className="text-foreground">Held.</span> This branch has not started. Send a direction
        below to launch it.
      </p>
    );
  if (notice === "queued") return <QueuedNotice className={COL} />;
  if (notice === "paused")
    // "Pausing… (Ada)" until the step reaches a safe point, then "Paused by Ada - resume
    // when…"; a step waiting for its device: "Waiting for MacBook Pro".
    return <PauseBanner className={COL} pause={pause} away={view.deviceAway?.title} />;
  if (notice === "working")
    // The live execution tree carries its own liveness; this line covers the pre-tree
    // stretch (clarifying/created) and replays without step frames. Connection truth
    // survives `waiting` - same law as the tree's.
    return (
      <WorkingLine
        className={COL}
        status={view.status}
        steps={view.steps}
        createdAt={createdAt}
        note={liveNote}
      />
    );
  return null;
}

/** The end of the transcript: the settled answer's actions, the pouring draft, the ONE tail
 *  line at the TRUE bottom, and the notice under it. Per-frame heartbeat renders land here,
 *  outside every memoized feed row. */
function TranscriptTail(p: TailProps) {
  const { view, live } = p;
  const liveExec = live ? liveExecOf(view.feed) : null;
  return (
    <>
      {/* the end of the response: the answer is done and it's yours - copy it, grade it, or
          run the ask again. Settled tasks only; a new turn folds these away the moment it
          opens. */}
      {!live && view.feed.length > 0 && (
        <MessageScrollerItem messageId="response-actions" className={COL}>
          <ResponseActions
            answerText={p.answerText}
            feedback={view.feedback}
            onFeedback={p.onFeedback}
            onRetry={p.onRetry}
            retrying={p.retrying}
          />
        </MessageScrollerItem>
      )}
      {view.draft && live && (
        <MessageScrollerItem messageId="draft" className={COL}>
          <DraftCard path={view.draft.path} text={view.draft.text} />
        </MessageScrollerItem>
      )}
      {/* While a draft pours, the phrase names the file being written. Connection truth
          survives `waiting`. */}
      {liveExec && (
        <MessageScrollerItem messageId="task-tail" className={COL}>
          <ExecTail
            item={liveExec}
            live={view.live}
            status={view.status}
            note={p.liveNote}
            pause={p.pause}
            gate={view.gates.at(-1)}
            draftName={view.draft ? view.draft.path.split("/").pop() || null : null}
          />
        </MessageScrollerItem>
      )}
      {live && !p.gated && <LiveNoticeRow {...p} />}
      {view.feed.length === 0 &&
        !live &&
        // Nothing has been replayed yet (appliedSeq 0) is "still loading", not "empty": the
        // stream's first frames land a moment after the page does.
        (view.appliedSeq === 0 ? (
          <LoadingState className={COL} label="Loading the transcript…" />
        ) : (
          <p className={cn(COL, "text-muted-foreground text-body-sm-regular")}>
            No transcript for this task.
          </p>
        ))}
    </>
  );
}

/**
 * The conversation. The scroller follows the tail while you're at the bottom, lets go the
 * moment you scroll up (wheel/touch/keys), and re-attaches when you come back. No negative
 * margins: the viewport must end where the rail begins, or its scrollbar renders underneath
 * the rail (the gutter keeps the scrollbar a breath away from the text).
 */
export function Transcript({
  markdownActions,
  scrollApi,
  ancestors,
  ancestorsError,
  feed,
  tail,
}: {
  /** Draft-card actions (Use this draft) resolve against THIS surface's composer - peeks and
   *  the inbox provide no handler, so their cards show copy only. */
  markdownActions: MarkdownActions;
  scrollApi: RefObject<ScrollApi | null>;
  ancestors: Ancestor[];
  ancestorsError: boolean;
  feed: TaskFeedProps;
  tail: TailProps;
}) {
  return (
    <div className="min-h-0 flex-1">
      <MarkdownActionsProvider value={markdownActions}>
        <MessageScrollerProvider autoScroll scrollEdgeThreshold={48}>
          <ScrollBridge apiRef={scrollApi} />
          <MessageScroller>
            <MessageScrollerViewport className="px-gutter">
              <MessageScrollerContent className="gap-4 pt-6 pb-(--bar)">
                <AncestorFeed
                  ancestors={ancestors}
                  ancestorsError={ancestorsError}
                  artifacts={feed.artifacts}
                  onOpenArtifact={feed.onOpenArtifact}
                  shared={feed.shared}
                />
                <TaskFeed {...feed} />
                <TranscriptTail {...tail} />
              </MessageScrollerContent>
            </MessageScrollerViewport>
            <MessageScrollerButton className="data-[direction=end]:bottom-(--bar)" />
          </MessageScroller>
        </MessageScrollerProvider>
      </MarkdownActionsProvider>
    </div>
  );
}
