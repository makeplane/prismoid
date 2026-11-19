/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { type CSSProperties, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { redirect, replace } from "react-router";
import { toast } from "@prismoid/blocks/toast";
import type { ComposerApi } from "~/components/composer/composer";
import { LoadingState, type MarkdownActions } from "~/components/kit";
import type { QuestionDockHandle } from "~/components/tasks/question-dock";
import { useTaskWindow } from "~/components/tasks/task-window";
import { currentLease } from "~/hooks/use-screen";
import { useSeenTask } from "~/hooks/use-seen-task";
import { useTaskStream } from "~/hooks/use-task-stream";
import { useTypingSender } from "~/hooks/use-typing-sender";
import { useViewer } from "~/hooks/use-viewer";
import { api, unwrap } from "~/lib/api/client";
import { TERMINAL } from "~/lib/api/events";
import { projectHref, taskRedirect } from "~/lib/links";
import { answeredFirst, presentPeople, typingPeople } from "~/lib/presence";
import { knownProjectName, rememberProjectName } from "~/lib/project-names";
import { seenKey } from "~/lib/seen-task";
import { gatesFor } from "~/lib/task-view";
import { cn } from "~/lib/utils";
import { ComposerBar } from "./composer-bar";
import type { PauseView } from "./progress-copy";
import type { Rating } from "./response-actions";
import { RightSide } from "./right-side";
import type { ScrollApi } from "./scroll-bridge";
import { TaskChrome, TaskHeader } from "./task-chrome";
import {
  answerTextOf,
  composerPlaceholder,
  earlierPlansOf,
  liveNoteOf,
  openGateOf,
  railMachineOf,
  railStepsOf,
  spendOf,
  usageOf,
  waitOf,
} from "./task-page";
import type { SourceInput } from "./task-rail";
import { Transcript } from "./transcript";
import { useAncestors } from "./use-ancestors";
import { useComposerSend } from "./use-composer-send";
import { useFollowUpDials } from "./use-follow-up-dials";
import { useLiveRefresh } from "./use-live-refresh";
import { useRightSurface } from "./use-right-surface";
import { useSteer } from "./use-steer";
import { useTaskActions } from "./use-task-actions";
import type { Route } from "./+types/route";

// The transcript row, re-exported for the render pin in route.test.tsx.
export { FeedRow } from "./feed-row";

export function meta({ loaderData }: Route.MetaArgs) {
  const name = loaderData?.detail?.title || loaderData?.detail?.goal;
  return [
    {
      title: name ? `${name.slice(0, 48)} · Prismoid` : "Task · Prismoid",
    },
  ];
}

// ONE page module, TWO routes (routes.app.ts): /tasks/:id and /projects/:projectId/tasks/:id.
// A Project task's canonical URL is the nested one; a personal task's is /tasks/:id. The
// URL's project segment is context, never authority: the task is read by its id alone and the
// server authorizes that, so the segment can be wrong, stale (the task moved) or garbage - the
// loader then redirects to the task's own canonical URL: REPLACING the entry when the bar already
// shows the wrong one (Back never bounces), PUSHING when the reader clicked here from another
// page (Back returns to it) - `taskRedirect` decides.
export async function clientLoader({ params, request }: Route.ClientLoaderArgs) {
  const projectSegment = "projectId" in params ? params.projectId : undefined;
  // The crumb's Project name: reuse one the Project page or the sidebar already holds, and only
  // otherwise ask - alongside the task rather than after it, since when the URL is right it is
  // the task's Project and the page needs it (when wrong, the answer is discarded and the loader
  // redirects). Best-effort: an unreadable Project degrades the crumb only.
  const known = projectSegment ? knownProjectName(projectSegment) : undefined;
  const [detail, fetched] = await Promise.all([
    unwrap(
      api.GET("/v1/tasks/{task_id}", {
        params: { path: { task_id: params.id } },
      }),
    ),
    projectSegment && known === undefined
      ? unwrap(
          api.GET("/v1/projects/{project_id}", {
            params: { path: { project_id: projectSegment } },
          }),
        ).catch(() => null)
      : Promise.resolve(null),
  ]);
  if (fetched) rememberProjectName(fetched.id, fetched.name);
  const url = new URL(request.url);
  const go = taskRedirect(
    { pathname: url.pathname, search: url.search },
    typeof window !== "undefined"
      ? { pathname: window.location.pathname, hash: window.location.hash }
      : null,
    detail,
  );
  if (go) throw go.replace ? replace(go.to) : redirect(go.to);
  const name = known ?? (fetched?.id === detail.project_id ? fetched.name : undefined);
  const project = detail.project_id && name !== undefined ? { id: detail.project_id, name } : null;
  return { detail, project };
}
clientLoader.hydrate = true as const;

// The breadcrumb says WHICH task - the minted (or user-set) title, else the ask. A Project task
// crumbs through its Project (a link back to the lobby); a personal task keeps the path's own
// "Tasks > title". When the task did not load (not found) there is no loader data, but the
// nested URL still names its Project: the trail stays live rather than falling back to path
// segments, one of which ("/projects/<id>/tasks") is no page.
export const handle = {
  crumb: (data: unknown) => {
    const d = data as { detail?: { title?: string | null; goal?: string } } | undefined;
    return d?.detail?.title || d?.detail?.goal || null;
  },
  trail: (data: unknown, params?: Record<string, string | undefined>) => {
    const d = data as
      | {
          detail?: {
            project_id?: string | null;
            title?: string | null;
            goal?: string;
          };
          project?: { name?: string } | null;
        }
      | undefined;
    if (!d?.detail) {
      const projectId = params?.projectId;
      return projectId
        ? [
            { label: "Projects", to: "/projects" },
            { label: "Project", to: projectHref(projectId) },
            { label: "Task" },
          ]
        : null;
    }
    const projectId = d.detail.project_id;
    if (!projectId) return null;
    return [
      { label: "Projects", to: "/projects" },
      { label: d.project?.name || "Project", to: projectHref(projectId) },
      { label: d.detail.title || d.detail.goal || "Task" },
    ];
  },
};
// A stable empty inputs array - a fresh `[]` in railProps each render defeats TaskRail's memo.
const EMPTY_INPUTS: SourceInput[] = [];

type ControlIntent = "pause" | "resume" | "cancel";

export default function TaskDetail({ loaderData }: Route.ComponentProps) {
  if (!loaderData) {
    return <LoadingState />;
  }
  // Keyed by task: sidebar navigation swaps params on a MOUNTED route, and an un-keyed
  // TaskChat carried the previous task's everything - one painted frame of the old
  // transcript (the flicker), the old scroll offset and detach state (the weird position),
  // stale ancestors, the rail sheet, an open artifact. A remount is the clean slate:
  // fresh view from this task's detail, fresh scroller pinned to the tail.
  return <TaskChat key={loaderData.detail.id} detail={loaderData.detail} />;
}

/** The floating bar's height is the transcript's bottom clearance (`--bar`): a staged question
 *  dock and the note above it can be twice the bare composer, and a fixed clearance left the
 *  transcript's tail beneath the bar - the steer note printed over the streaming draft. */
function useBarHeight() {
  const barRef = useRef<HTMLDivElement>(null);
  const [barHeight, setBarHeight] = useState(160);
  useEffect(() => {
    const bar = barRef.current;
    if (!bar) return;
    const observer = new ResizeObserver(() => setBarHeight(bar.offsetHeight));
    observer.observe(bar);
    return () => observer.disconnect();
  }, []);
  return { barRef, barHeight };
}

/**
 * The task page: the stream's view, composed. Each concern lives in its own hook - the live
 * refresh of the loader snapshot (use-live-refresh), the viewer's inbox items about the task read
 * as they see it (use-seen-task), the right side's one surface
 * (use-right-surface), the follow-up dials (use-follow-up-dials), the steer and its stamp
 * (use-steer), the box's send (use-composer-send) and the one-shot verbs (use-task-actions) -
 * and each region renders in its own component: the chrome, the transcript, the composer bar
 * and the right side. The small decisions are pure in task-page.ts.
 */
function TaskChat({
  detail,
}: {
  detail: NonNullable<Route.ComponentProps["loaderData"]>["detail"];
}) {
  const { view, connected, stalled, stalledLong, foldLocal, presence, conn, gone } = useTaskStream(
    detail.id,
    detail,
  );
  // Who is looking. `detail.project_id` is what makes a task shared: only then does the
  // timeline name its authors and draw its control lines - a personal task reads exactly as
  // it always did.
  const shared = detail.project_id != null;
  const canEdit = detail.can_edit;
  // A viewer reads along: every write the page offers is an editor's, absent otherwise.
  const editorOnly = <T,>(write: T): T | undefined => (canEdit ? write : undefined);
  const viewer = useViewer();
  const myId = viewer?.id;
  const myName = viewer?.name;
  // Two people can answer one question; the second is handed the first's resolution (not an
  // error) and told whose it was. The gate.resolved frame draws "Bob answered".
  const noteFirst = useCallback(
    (resolvedBy?: string, resolvedById?: string | null, action?: string) => {
      const note = answeredFirst(resolvedBy, myName, { resolvedById, myId, action });
      if (note) toast(note);
    },
    [myName, myId],
  );
  // The open gates as this viewer may answer them: a Project editor is never offered `always`.
  const gates = useMemo(() => gatesFor(view, myId), [view, myId]);
  const live = !TERMINAL.has(view.status);
  const { gate, questionOpen, steerNote, controlGate } = openGateOf(view, gates, live, myId);
  // A HELD fork: born `created` + held_at, parked until the first directive. Launch enqueues
  // the run, the stream moves status past created, and this flag falls false on its own.
  const held = detail.held_at != null && view.status === "created";
  // Legacy "council" rows are fleet-family everywhere the server guards.
  const fleetThread = detail.mode === "fleet" || detail.mode === "council";

  const { barRef, barHeight } = useBarHeight();
  const dockRef = useRef<QuestionDockHandle>(null);
  const scrollApi = useRef<ScrollApi | null>(null);
  // "Use this draft" on a rendered email/message card stages the draft in the composer: the
  // person reviews and sends it as the thread's next turn - the card never sends anything
  // itself. Stable value: a fresh object each render would pierce every settled FeedRow's memo
  // through the context.
  const composerApi = useRef<ComposerApi | null>(null);
  const markdownActions = useMemo<MarkdownActions>(
    () => ({
      onUseDraft: (draft) => {
        composerApi.current?.insert(draft);
        scrollApi.current?.toEnd();
      },
    }),
    [],
  );

  // The sidebar reads this task through the window - push what the stream knows (status, the
  // minted title) so its row settles the moment the task does, not on the next navigation
  // refetch.
  const { patch: patchWindow } = useTaskWindow();
  useEffect(() => {
    patchWindow(detail.id, {
      status: view.status,
      ending: view.ending,
      ...(view.title ? { label: view.title } : {}),
    });
  }, [patchWindow, detail.id, view.status, view.ending, view.title]);

  const revalidator = useLiveRefresh({ detail, view, connected, fleetThread, foldLocal });
  useSeenTask(detail.id, seenKey(view));
  // The stream learned the task is not this viewer's any more (removed from its Project, or
  // deleted): reload the page's data, so the route answers its own not-found and the sidebar
  // drops what this person can no longer open. The task fetch path still says only NotFound.
  useEffect(() => {
    if (gone) void revalidator.revalidate();
  }, [gone, revalidator]);
  const { ancestors, ancestorsError } = useAncestors(detail.parent_task_id, view.parentTaskId);
  // Memoized so settled FeedRows (React.memo) skip re-rendering on every streamed delta.
  const allArtifacts = useMemo(
    () => [...ancestors.flatMap((a) => a.artifacts), ...view.artifacts],
    [ancestors, view.artifacts],
  );
  const surface = useRightSurface({
    machine: detail.machine,
    artifacts: view.artifacts,
    allArtifacts,
    live,
    superseded: view.superseded,
  });
  const actions = useTaskActions({
    task: detail,
    feedback: view.feedback,
    foldLocal,
    revalidate: () => void revalidator.revalidate(),
  });

  // Typing frames: editors of a SHARED task only, best-effort (hooks/use-typing-sender.ts).
  const sendTyping = useTypingSender({
    taskId: detail.id,
    conn,
    enabled: shared && canEdit,
    live,
    gate,
  });
  const dials = useFollowUpDials({ live, fleetThread });
  const { setDraft } = dials;
  const onText = useCallback(
    (text: string) => {
      setDraft(text);
      sendTyping(text);
    },
    [setDraft, sendTyping],
  );
  const steer = useSteer({
    taskId: detail.id,
    steerEpoch: view.steerEpoch,
    held,
    foldLocal,
    onText,
    scrollApi,
    composerApi,
  });
  const send = useComposerSend({
    task: detail,
    live,
    questionOpen,
    steerEpoch: view.steerEpoch,
    foldLocal,
    dials,
    steer,
    dockRef,
    scrollApi,
    composerApi,
  });

  // The pause as this page tells it: a shared task names who paused; on a personal task the
  // only one pausing is you, and the name would only repeat it. Memoised - the tail and the
  // status line read it on every streamed frame.
  const pause = useMemo<PauseView | null>(
    () => (view.pause ? { by: shared ? view.pause.by : "", applied: view.pause.applied } : null),
    [view.pause, shared],
  );
  const answerText = useMemo(
    () => answerTextOf(view.deliveredSummary, view.feed),
    [view.deliveredSummary, view.feed],
  );
  const earlierPlans = useMemo(
    () => earlierPlansOf(detail.id, view.planHistory, ancestors),
    [view.planHistory, ancestors, detail.id],
  );
  const spendUsd = spendOf(view);
  // Memoized so a streamed delta doesn't mint a fresh `usage` object every render and defeat
  // TaskRail's memo - it changes only when the spend or the call count does.
  const usage = useMemo(
    () => usageOf(spendUsd, view.usageCalls, view.usageSearches),
    [spendUsd, view.usageCalls, view.usageSearches],
  );
  // The control lease as the page draws it: the reducer's `screen` (seeded from the task read,
  // then every `screen.state`, reseeded from the task read after each). Never expired on the
  // client's clock.
  const lease = currentLease(view.screen, Date.now());
  const railMachine = useMemo(
    () => railMachineOf(detail.machine, lease),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `lease` is derived from view.screen
    [detail.machine, view.screen],
  );
  const railSteps = useMemo(() => railStepsOf(view.steps, view.status), [view.steps, view.status]);
  const railProps = {
    steps: railSteps,
    settled: !live,
    earlierPlans,
    artifacts: allArtifacts,
    inputs: detail.inputs ?? EMPTY_INPUTS,
    usage,
    context: view.context,
    machine: railMachine,
    canEdit: detail.can_edit,
    onSleepMachine: () => void actions.machineAction("sleep"),
    onResetMachine: () => void actions.machineAction("reset"),
    taskId: detail.id,
    screenHolder: lease?.holder.name ?? null,
    onOpenScreen: surface.openScreenPane,
    onOpenArtifact: surface.openArtifactFromRail,
    superseded: view.superseded,
  };

  const taskChrome = (
    <TaskChrome
      detail={detail}
      status={view.status}
      ending={view.ending}
      spendUsd={spendUsd}
      calls={view.usageCalls}
      searches={view.usageSearches}
      people={presentPeople(presence, Date.now(), conn)}
      artifactMode={surface.artifactMode}
      railPressed={surface.wide ? surface.railPref : surface.railSheet}
      onShowProgress={() => surface.setRailModal(true)}
      onToggleRail={surface.toggleRail}
      onFork={editorOnly(() => void actions.fork())}
      onControl={editorOnly((intent: ControlIntent) => void actions.control(intent))}
    />
  );

  return (
    // An exact viewport row, full-bleed: this route sits outside the shell's page container,
    // so the rail anchors to the window edge and the document itself never scrolls - the
    // transcript and the rail each own their scrollbar, streaming can't move the page, and
    // the composer never floats over text. In ARTIFACT MODE the shared topbar yields, so the
    // row owns the FULL viewport height and the conversation and the artifact both run from
    // the very top of the window, the chat with its own h-14 band.
    <div
      ref={surface.rowRef}
      className={cn(
        "min-w-0",
        surface.artifactMode
          ? // GRID: minmax COLUMN tracks arbitrate chat-floor vs pane-share (reader-pane.tsx;
            // the template itself is written by use-right-surface, never a style prop). The
            // ROW track must be pinned too - an implicit row is content-sized, so both panes
            // grew instead of scrolling.
            "grid h-svh grid-rows-[minmax(0,1fr)]"
          : "flex h-[calc(100svh-3.5rem)] flex-row",
      )}
    >
      {/* the task column - relative: it anchors the floating composer. Its floor in artifact
          mode is the grid track's minmax, not a min-w class (flex-1 serves the flex mode; a
          grid item ignores it). */}
      <div
        className="relative flex min-w-0 flex-1 flex-col"
        style={{ "--bar": `${barHeight}px` } as CSSProperties}
      >
        <TaskHeader
          artifactMode={surface.artifactMode}
          detail={detail}
          title={view.title}
          chrome={taskChrome}
        />

        <Transcript
          markdownActions={markdownActions}
          scrollApi={scrollApi}
          ancestors={ancestors}
          ancestorsError={ancestorsError}
          feed={{
            view,
            artifacts: allArtifacts,
            onOpenArtifact: surface.showArtifact,
            shared,
            taskInputs: detail.inputs,
            questionOpen,
            myId,
            // Start is a follow-up: a viewer is shown the card, not the button
            onStartProposal: editorOnly(send.startProposal),
            onRetract: editorOnly(actions.retractSteer),
            onDeclineProposal: actions.declineProposalRow,
          }}
          tail={{
            view,
            live,
            gated: gate != null,
            held,
            createdAt: detail.created_at,
            liveNote: liveNoteOf(connected, stalled, stalledLong, waitOf(view)),
            pause,
            answerText,
            retrying: actions.retrying,
            onFeedback: editorOnly((rating: Rating) => void actions.sendFeedback(rating)),
            onRetry: editorOnly(() => void actions.retry()),
          }}
        />

        <ComposerBar
          barRef={barRef}
          detail={detail}
          view={view}
          gate={gate}
          live={live}
          held={held}
          steerNote={steerNote}
          spendUsd={spendUsd}
          typers={typingPeople(presence, Date.now(), myId)}
          stale={steer.stale}
          sending={send.sending}
          dials={dials}
          dockRef={dockRef}
          composerApi={composerApi}
          placeholder={composerPlaceholder({ questionOpen, held, live })}
          onResolved={noteFirst}
          onOpenScreen={surface.openScreenPane}
          onDraft={steer.onDraft}
          onSubmit={send.submit}
          onStop={() => void actions.control("cancel")}
          onSendAnyway={() => void send.sendAnyway()}
          onDiscardStale={steer.discardStale}
        />
      </div>

      <RightSide
        surface={surface}
        detail={detail}
        view={view}
        lease={lease}
        controlGate={controlGate}
        myId={myId}
        railProps={railProps}
      />
    </div>
  );
}

export { RouteErrorBoundary as ErrorBoundary } from "~/components/layout/route-error";
