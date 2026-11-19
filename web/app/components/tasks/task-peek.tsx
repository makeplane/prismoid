/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The task peek - a run opened in place, never a navigation away (the inbox law: read and
// finish where you are). Promoted from routes/bots on its second cross-route consumer
// (/routines' run peeks) - route-local components never cross routes.
// `TaskPeekContent` is the composable half: /routines swaps it INTO its own sheet with a
// back header, exactly as the bots peek swaps in the artifact reader.
// Original note:
// read and finish in place; the projects-shelf precedent: peek + the reader drawer). It folds
// the task's live stream through the same reducer and draws the same thread grammar as the
// chat, so a peeked run reads exactly like a conversation. Gates stay answerable here; the
// full task page is the one-click escalation, exactly as full-screen is for an artifact.

import { ArrowLeft, Maximize2, X } from "lucide-react";
import { useEffect, useState } from "react";
import { Link } from "react-router";
import { PeekSheet } from "~/components/artifact/artifact-reader";
import { ArtifactView } from "~/components/artifact/artifact-view";
import { Liveness, LoadingState } from "~/components/kit";
import { FeedRow } from "~/components/tasks/feed-row";
import { GateDock } from "~/components/tasks/gate-dock";
import { DrawerTitle } from "@makeplane/propel/components/drawer";
import { Icon } from "@makeplane/propel/components/icon";
import { IconButton } from "@makeplane/propel/components/icon-button";
import { api, toastError, unwrap } from "~/lib/api/client";
import { taskHref } from "~/lib/links";
import { useTaskStream } from "~/hooks/use-task-stream";
import type { components } from "~/lib/api/types.gen";
import type { GateView } from "~/lib/task-view";

type TaskDetailOut = components["schemas"]["TaskDetailOut"];

export function TaskPeek({ taskId, onClose }: { taskId: string | null; onClose: () => void }) {
  return (
    <PeekSheet open={taskId !== null} onOpenChange={(o) => !o && onClose()}>
      {/* Base UI's Drawer.Title is its Dialog.Title, so this names the peek whichever sheet hosts it. */}
      <div className="sr-only">
        <DrawerTitle>Task</DrawerTitle>
      </div>
      {taskId !== null && <TaskPeekContent key={taskId} taskId={taskId} onClose={onClose} />}
    </PeekSheet>
  );
}

/** Fetch once, then hand the stream a seeded view - the hook wants the detail up front. */
export function TaskPeekContent({ taskId, onClose }: { taskId: string; onClose: () => void }) {
  const [detail, setDetail] = useState<TaskDetailOut | null>(null);
  useEffect(() => {
    let alive = true;
    void unwrap(api.GET("/v1/tasks/{task_id}", { params: { path: { task_id: taskId } } })).then(
      (d) => alive && setDetail(d),
      (e) => alive && toastError(e, "Couldn't open this run."),
    );
    return () => {
      alive = false;
    };
  }, [taskId]);
  if (detail === null) return <LoadingState label="Opening the run…" />;
  return <PeekThread taskId={taskId} detail={detail} onClose={onClose} />;
}

/** The peek's gate face: editors only, like the page (a viewer reads the tree), through the one
 *  switch every host mounts - the peek has the task id, Project id and spend the budget face needs. */
export function PeekGateDock({
  gate,
  taskId,
  projectId,
  spentUsd,
  canEdit,
  isCreator,
  creatorName,
}: {
  gate: GateView;
  taskId: string;
  projectId: string | null;
  spentUsd: number;
  canEdit: boolean;
  isCreator: boolean;
  creatorName?: string | null;
}) {
  if (!canEdit) return null;
  return (
    <GateDock
      gate={gate}
      isCreator={isCreator}
      creatorName={creatorName}
      budget={{ taskId, projectId, spentUsd }}
    />
  );
}

function PeekThread({
  taskId,
  detail,
  onClose,
}: {
  taskId: string;
  detail: TaskDetailOut;
  onClose: () => void;
}) {
  const { view } = useTaskStream(taskId, detail);
  const working = view.status === "running" || view.status === "planning";
  // An artifact card inside the peek swaps the pane to the reader - ← returns to the thread.
  const [openArtifact, setOpenArtifact] = useState<string | null>(null);

  if (openArtifact !== null) {
    return (
      <div className="scrollbar-thin min-h-0 min-w-0 flex-1 overflow-x-hidden overflow-y-auto overscroll-contain px-4 pt-4 pb-6">
        <ArtifactView
          artifactId={openArtifact}
          onArtifactChanged={setOpenArtifact}
          readOnly={!detail.can_edit}
          hostActions={
            <>
              <IconButton
                variant="ghost"
                size="sm"
                icon={<Icon icon={ArrowLeft} />}
                aria-label="Back to the run"
                onClick={() => setOpenArtifact(null)}
              />
              <IconButton
                nativeButton={false}
                variant="ghost"
                size="sm"
                icon={<Icon icon={Maximize2} />}
                aria-label="Full screen"
                render={<Link to={`/artifacts/${openArtifact}`} />}
              />
              <IconButton
                variant="ghost"
                size="sm"
                icon={<Icon icon={X} />}
                aria-label="Close"
                onClick={onClose}
              />
            </>
          }
        />
      </div>
    );
  }

  return (
    <>
      <header className="flex items-center gap-2 border-b px-4 py-3">
        <div className="min-w-0 flex-1">
          <p className="truncate text-body-sm-regular font-medium">
            {view.title ?? detail.title ?? "Task"}
          </p>
          <p className="text-muted-foreground text-caption-md-regular capitalize">{view.status}</p>
        </div>
        <IconButton
          nativeButton={false}
          variant="ghost"
          size="sm"
          icon={<Icon icon={Maximize2} />}
          aria-label="Open the full task"
          render={<Link to={taskHref(detail)} />}
        />
        <IconButton
          variant="ghost"
          size="sm"
          icon={<Icon icon={X} />}
          aria-label="Close"
          onClick={onClose}
        />
      </header>
      <div className="scrollbar-thin flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto px-5 py-4">
        {view.feed.map((item) => (
          <FeedRow key={item.id} item={item} onOpenArtifact={setOpenArtifact} />
        ))}
        {view.gates[0] && (
          <PeekGateDock
            key={view.gates[0].gate_id}
            gate={view.gates[0]}
            taskId={taskId}
            projectId={detail.project_id ?? null}
            // the live ledger figure, as the task page's dock reads it (D-I3)
            spentUsd={Number(view.usageUsd ?? view.totalUsd ?? 0)}
            canEdit={detail.can_edit}
            isCreator={detail.is_creator}
            creatorName={detail.creator_name}
          />
        )}
        {working && (
          <div className="text-muted-foreground flex items-center gap-2 text-caption-md-regular">
            <Liveness className="size-3.5" /> working…
          </div>
        )}
      </div>
    </>
  );
}
