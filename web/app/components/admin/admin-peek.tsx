/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The admin task peek - the owner's thread, read from the durable record. Admins have no
// SSE lane, so this pages GET /v1/admin/tasks/{task_id}/events (raw wire payloads) and folds
// them through the SAME pure reducer replay uses (lib/admin-events → foldEvents): the
// thread an operator reads can never disagree with the one the owner streamed. While the
// task is live it re-polls incrementally (?since = the highest seq seen; the reducer's seq
// dedup makes overlap harmless) every 8s. Cancel is the one verb - reasoned, confirmed,
// audited; opening the peek itself is audited server-side (the detail GET).
//
// Addressed by id, not row: the console hands `taskId` off the URL (?task=) plus a row
// `seed` when the list happens to hold it - a direct load without one simply renders from
// the audited detail fetch. The owner's email is a Link to their operator page; navigating
// there closes the peek naturally (the param dies with the URL).

import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router";
import { toast } from "@prismoid/blocks/toast";
import { PeekSheet } from "~/components/artifact/artifact-reader";
import { Liveness, LoadingState, Money, StatusBadge } from "~/components/kit";
import { FeedRow } from "~/components/tasks/feed-row";
import { ReasonDialog } from "./confirm";
import { Icon } from "@makeplane/propel/components/icon";
import { IconButton } from "@makeplane/propel/components/icon-button";
import { DrawerTitle } from "@makeplane/propel/components/drawer";
import { X } from "lucide-react";
import { api, toastError, unwrap } from "~/lib/api/client";
import { TERMINAL, type TaskStatus } from "~/lib/api/events";
import { foldAdminEvents, maxSeq } from "~/lib/admin-events";
import type { TaskView } from "~/lib/task-view";
import type { AdminTaskRow } from "./task-console";

const POLL_MS = 8_000;
const PAGE_LIMIT = 500;
const MAX_PAGES = 40; // a runaway record must not loop the browser forever

/** Drain the record from `since` (exclusive) - pages until the server says short. */
async function fetchEventsSince(taskId: string, since: number): Promise<unknown[]> {
  const acc: unknown[] = [];
  let cursor = since;
  for (let i = 0; i < MAX_PAGES; i++) {
    const page = await unwrap(
      api.GET("/v1/admin/tasks/{task_id}/events", {
        params: {
          path: { task_id: taskId },
          query: { since: cursor, limit: PAGE_LIMIT },
        },
      }),
    );
    acc.push(...page.events);
    if (page.next_since === null) break;
    cursor = page.next_since;
  }
  return acc;
}

export function AdminTaskPeek({
  taskId,
  seed,
  onClose,
}: {
  taskId: string | null;
  seed?: AdminTaskRow;
  onClose: () => void;
}) {
  return (
    <PeekSheet open={taskId !== null} onOpenChange={(o) => !o && onClose()}>
      {/* DrawerTitle is Base UI's Dialog.Title, so it labels the kit sheet and a Propel drawer alike. */}
      <div className="sr-only">
        <DrawerTitle>Task</DrawerTitle>
      </div>
      {taskId !== null && <PeekBody key={taskId} taskId={taskId} seed={seed} onClose={onClose} />}
    </PeekSheet>
  );
}

function PeekBody({
  taskId,
  seed,
  onClose,
}: {
  taskId: string;
  seed?: AdminTaskRow;
  onClose: () => void;
}) {
  // The accumulated raw record - a ref (append-only), refolded into state on every batch.
  const eventsRef = useRef<unknown[]>([]);
  const sinceRef = useRef(0);
  const [view, setView] = useState<TaskView | null>(null);
  // The audited detail read - also the status fallback before any frame folds. Seeded from
  // the console's row when the list holds it; null on a direct ?task= load until it lands.
  const [detail, setDetail] = useState<AdminTaskRow | null>(seed ?? null);

  const pull = useCallback(async () => {
    const fresh = await fetchEventsSince(taskId, sinceRef.current);
    if (fresh.length > 0) {
      eventsRef.current = [...eventsRef.current, ...fresh];
      sinceRef.current = Math.max(sinceRef.current, maxSeq(fresh));
    }
    setView(foldAdminEvents(eventsRef.current));
  }, [taskId]);

  useEffect(() => {
    let alive = true;
    void unwrap(
      api.GET("/v1/admin/tasks/{task_id}", {
        params: { path: { task_id: taskId } },
      }),
    ).then(
      (d) => alive && setDetail(d),
      () => {}, // a row seed stands; without one, the record's own toast tells the story
    );
    pull().catch((e) => {
      if (alive) toastError(e, "Couldn't read this task's record.");
    });
    return () => {
      alive = false;
    };
  }, [taskId, pull]);

  // Fall back to the (fresher of) row/detail status until frames fold a status of their own.
  const status =
    view && view.appliedSeq > 0 ? view.status : (detail?.status as TaskStatus | undefined);
  const live = status !== undefined && !TERMINAL.has(status as TaskStatus);

  useEffect(() => {
    if (!live) return;
    const id = window.setInterval(() => {
      if (document.hidden) return;
      void pull().catch(() => {});
    }, POLL_MS);
    return () => window.clearInterval(id);
  }, [live, pull]);

  if (detail === null) {
    // No seed and the audited read hasn't landed - nothing honest to headline yet.
    return <LoadingState label="Reading the task…" />;
  }

  return (
    <>
      <header className="flex items-center gap-2 border-b px-4 py-3">
        <div className="min-w-0 flex-1">
          <p className="truncate text-body-sm-regular font-medium">
            {view?.title ?? detail.title ?? detail.goal_line}
          </p>
          <p className="text-muted-foreground truncate text-caption-md-regular">
            {detail.owner ? (
              <Link
                to={`/users/${detail.owner.id}`}
                className="hover:text-foreground hover:underline"
              >
                {detail.owner.email}
              </Link>
            ) : (
              "no owner"
            )}{" "}
            · <Money usd={detail.spend_usd} muted /> · turn {detail.turn}
          </p>
        </div>
        {status !== undefined && <StatusBadge status={status} />}
        {live && (
          <CancelDialog
            taskId={taskId}
            onCancelled={(after) => {
              setDetail(after);
              void pull().catch(() => {});
            }}
          />
        )}
        <IconButton
          variant="ghost"
          size="sm"
          icon={<Icon icon={X} />}
          aria-label="Close"
          onClick={onClose}
        />
      </header>
      {view === null ? (
        <LoadingState label="Reading the record…" />
      ) : (
        <div className="scrollbar-thin flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto px-5 py-4">
          {view.feed.length === 0 && (
            <p className="text-muted-foreground text-body-sm-regular">No durable frames yet.</p>
          )}
          {view.feed.map((item) => (
            // No onOpenArtifact: an admin can't presign another user's artifacts, so the
            // card is a fact, not a dead door (the capability law).
            <FeedRow key={item.id} item={item} />
          ))}
          {live && (
            <div className="text-muted-foreground flex items-center gap-2 text-caption-md-regular">
              <Liveness className="size-3.5" /> live - re-reading every {POLL_MS / 1000}s
            </div>
          )}
        </div>
      )}
    </>
  );
}

/** Cancel takes a reason - it lands in the audit row the owner can be answered with. The same
 *  reasoned dialog the suspend verb uses (components/admin/confirm): two operator acts that
 *  both put prose on the record, one implementation of asking for it. */
function CancelDialog({
  taskId,
  onCancelled,
}: {
  taskId: string;
  onCancelled: (after: AdminTaskRow) => void;
}) {
  return (
    <ReasonDialog
      trigger="Cancel task"
      title="Cancel this task?"
      description="Stops the run at the next boundary. The reason is recorded in the audit trail; the owner can be answered with it."
      placeholder="Why an operator is stopping this run."
      confirmLabel="Cancel task"
      onConfirm={async (reason) => {
        const after = await unwrap(
          api.POST("/v1/admin/tasks/{task_id}/cancel", {
            params: { path: { task_id: taskId } },
            body: { reason },
          }),
        );
        toast.success(`Cancelled. Status is now ${after.status}.`);
        onCancelled(after);
      }}
    />
  );
}
