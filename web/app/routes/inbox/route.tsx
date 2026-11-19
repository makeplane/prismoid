/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import {
  AlarmClock,
  AlertTriangle,
  ArrowRight,
  Bot,
  CheckCheck,
  CheckCircle2,
  CircleDashed,
  CircleDollarSign,
  CircleSlash,
  FolderKanban,
  HandHelping,
  HelpCircle,
  Inbox as InboxIcon,
  ListFilter,
  MessageCircleQuestion,
  PauseCircle,
  Plug,
  SlidersHorizontal,
} from "lucide-react";
import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from "react";
import { useNavigate, useRevalidator } from "react-router";
import { ArtifactCard } from "~/components/artifact/artifact-card";
import { ArtifactReader } from "~/components/artifact/artifact-reader";
import {
  EmptyState,
  LabeledDivider,
  LoadingState,
  Markdown,
  Money,
  StatusBadge,
  StepTimeline,
  UserBubble,
} from "~/components/kit";
import { TopbarPortal } from "~/components/layout/topbar-slot";
import { GateCard } from "~/components/tasks/gate-card";
import { controlGateFor } from "~/lib/machine-copy";
import { Button } from "@makeplane/propel/components/button";
import { Icon } from "@makeplane/propel/components/icon";
import { IconButton } from "@makeplane/propel/components/icon-button";
import {
  Menu,
  MenuCheckboxItem,
  MenuContent,
  MenuRadioGroup,
  MenuRadioItem,
  MenuTrigger,
} from "@makeplane/propel/components/menu";
import { api, unwrap } from "~/lib/api/client";
import { TERMINAL } from "~/lib/api/events";
import { useViewer } from "~/hooks/use-viewer";
import { foldEvents, replayTaskEvents } from "~/lib/replay";
import { deliveredArtifacts, gatesFor, type TaskView } from "~/lib/task-view";
import { ago } from "~/lib/format";
import { taskHref } from "~/lib/links";
import { STORAGE } from "~/lib/storage";
import { cn } from "~/lib/utils";
import {
  botTarget,
  connectorTarget,
  noQuestionsLine,
  openLabel,
  projectTarget,
  routineTarget,
} from "./target";
import type { Route } from "./+types/route";

// The list pane is a real pane: draggable width (the divider IS the slider), persisted,
// double-click restores the default. The topbar's divider + icon cluster ride the same
// value, so the strip can never drift from the pane again.
const PANE_MIN = 300;
const PANE_MAX = 620;
const PANE_DEFAULT = 408; // = the old w-96 at the 17px root
const clampPane = (w: number) => Math.min(PANE_MAX, Math.max(PANE_MIN, w));

export function meta() {
  return [{ title: "Inbox · Prismoid" }];
}

export async function clientLoader() {
  return await unwrap(api.GET("/v1/inbox"));
}
clientLoader.hydrate = true as const;

type Item = Awaited<ReturnType<typeof clientLoader>>["items"][number];

// The wire kinds, with the glyph and the human word for each. "All" and "Unread" are views,
// not kinds - the chips mix both, the way Linear's inbox filters read.
const KIND_META: Record<string, { label: string; icon: typeof HelpCircle }> = {
  task_completed: { label: "Delivered", icon: CheckCircle2 },
  // Approval gates write this kind; the item points at its gate
  // and the task pane answers it in place.
  gate_opened: { label: "Approvals", icon: HelpCircle },
  // A running step asked a question; the item points at its gate, answered in place like an approval.
  question: { label: "Questions", icon: MessageCircleQuestion },
  escalation: { label: "Escalations", icon: AlertTriangle },
  budget: { label: "Budget", icon: CircleDollarSign },
  project_shared: { label: "Projects", icon: FolderKanban },
  // Removed from a Project: no open affordance - the room is not this person's any more.
  project_removed: { label: "Projects", icon: FolderKanban },
  // Someone other than the owner asked a bot for work; Open lands on the bot's chat, where the
  // card's Start / Not now decide it.
  card_waiting: { label: "Bot requests", icon: Bot },
  // Routines: the silence rule's outputs. Full /routines surface lands at W5;
  // these keep the kinds honest in the inbox meanwhile.
  routine_run: { label: "Routine runs", icon: AlarmClock },
  routine_paused: { label: "Routine paused", icon: PauseCircle },
  routine_held: { label: "Held runs", icon: HandHelping },
  routine_retired: { label: "Routine retired", icon: CheckCheck },
  // A connector's credential died between tasks: the open affordance lands on
  // the connectors page with the row's peek open - reconnect is one click from the knock.
  connector_broken: { label: "Connectors", icon: Plug },
};

// A settled task's knock wears what its answer said it did (`payload.ending`): a declined or partial
// answer never reads "Delivered" under a check.
const SETTLED_META: Record<string, { label: string; icon: typeof HelpCircle }> = {
  partial: { label: "Partly done", icon: CircleDashed },
  could_not: { label: "Couldn't do it", icon: CircleSlash },
};

function metaOf(item: Item) {
  const ending = (item.payload as { ending?: string } | null)?.ending;
  return (item.kind === "task_completed" && ending && SETTLED_META[ending]) || KIND_META[item.kind];
}

// The kinds that point at a gate the pane can answer in place.
const GATE_KINDS = new Set(["gate_opened", "question"]);

/** One line of plain text from a markdown body - a row shows a snippet, not a document. */
function plain(md: string): string {
  return md
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/[#*_`~>]+/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

export default function InboxRoute({ loaderData }: Route.ComponentProps) {
  const navigate = useNavigate();
  const revalidator = useRevalidator();
  const [items, setItems] = useState<Item[]>(loaderData.items);
  // Linear's two dimensions: Filter says WHAT (a kind), Display says HOW (unread only).
  const [kind, setKind] = useState<string>("all");
  const [unreadOnly, setUnreadOnly] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(loaderData.items[0]?.id ?? null);

  // The pane slider (the artifact reader's idiom): pointer-captured drag, clamped,
  // committed to localStorage on release; the topbar pieces read the same width.
  const rowRef = useRef<HTMLDivElement | null>(null);
  const [paneW, setPaneW] = useState(() => {
    if (typeof window === "undefined") return PANE_DEFAULT;
    const saved = Number(localStorage.getItem(STORAGE.inboxPane));
    return saved > 0 ? clampPane(saved) : PANE_DEFAULT;
  });
  const commitPane = (w: number) => {
    setPaneW(w);
    localStorage.setItem(STORAGE.inboxPane, String(w));
  };
  const paneFromPointer = (clientX: number) =>
    clampPane(clientX - (rowRef.current?.getBoundingClientRect().left ?? 0));
  const beginPaneResize = (e: ReactPointerEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.currentTarget.setPointerCapture(e.pointerId);
    document.body.style.userSelect = "none";
    document.body.style.cursor = "col-resize";
  };
  const paneResize = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (!e.currentTarget.hasPointerCapture(e.pointerId)) return;
    setPaneW(paneFromPointer(e.clientX));
  };
  const endPaneResize = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (!e.currentTarget.hasPointerCapture(e.pointerId)) return;
    e.currentTarget.releasePointerCapture(e.pointerId);
    document.body.style.userSelect = "";
    document.body.style.cursor = "";
    commitPane(paneFromPointer(e.clientX));
  };

  const unreadCount = items.filter((i) => !i.read).length;
  const kindsPresent = useMemo(() => new Set(items.map((i) => i.kind)), [items]);
  const visible = useMemo(
    () => items.filter((i) => (kind === "all" || i.kind === kind) && (!unreadOnly || !i.read)),
    [items, kind, unreadOnly],
  );
  const selected = visible.find((i) => i.id === selectedId) ?? null;

  async function markRead(id: string) {
    setItems((cur) => cur.map((i) => (i.id === id && !i.read ? { ...i, read: true } : i)));
    await unwrap(
      api.POST("/v1/inbox/{item_id}/read", {
        params: { path: { item_id: id } },
      }),
    ).catch(() => {});
    revalidator.revalidate(); // the sidebar badge reads the shell loader
  }

  async function markAll() {
    setItems((cur) => cur.map((i) => ({ ...i, read: true })));
    await unwrap(api.POST("/v1/inbox/read-all", {})).catch(() => {});
    revalidator.revalidate();
  }

  function select(item: Item) {
    setSelectedId(item.id);
    if (!item.read) void markRead(item.id);
  }

  function openTarget(item: Item) {
    const project = projectTarget(item); // a project_shared knock names its Project in its payload
    if (item.task_id) navigate(taskHref({ id: item.task_id, project_id: item.project_id }));
    else if (project) navigate(`/projects/${project}`);
    else {
      const target = connectorTarget(item) ?? botTarget(item) ?? routineTarget(item);
      if (target) navigate(target);
    }
  }

  // Linear's keyboard: ↑/↓ moves (and reads) the selection, Enter opens the thing itself.
  // The handler reads live selection state, so it rides a ref - the listener binds ONCE
  // instead of re-subscribing on every render (the missing dep array); a bare
  // `[]` would have stapled the first render's stale `visible`/`selected` onto the closure.
  const onKeyRef = useRef<(e: KeyboardEvent) => void>(() => {});
  onKeyRef.current = (e: KeyboardEvent) => {
    if (e.target instanceof HTMLElement) {
      const tag = e.target.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || e.target.isContentEditable) return;
    }
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp" && e.key !== "Enter") return;
    const idx = visible.findIndex((i) => i.id === selectedId);
    if (e.key === "Enter") {
      if (selected) openTarget(selected);
      return;
    }
    e.preventDefault();
    const next =
      e.key === "ArrowDown"
        ? visible[Math.min(idx + 1, visible.length - 1)]
        : visible[Math.max(idx - 1, 0)];
    if (next) select(next);
  };
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => onKeyRef.current(e);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return (
    // Full-bleed two-pane, Linear-shaped: the notification list on the left, the thing the
    // notification is about on the right. The page itself never scrolls.
    <div ref={rowRef} className="flex h-[calc(100svh-3.5rem)] min-w-0 flex-row">
      {/* Linear's strip, our shell: the pane controls - Filter (what kind), Display
          (unread only), Mark all read - RIGHT-ALIGN at the list pane's edge inside the
          topbar (absolute against the sticky header, pinned to the pane's live width),
          and the pane divider continues up through the strip (left: paneW - 1 sits the
          hairline exactly OVER the pane's border-r - same 408 line, not 1px beside it).
          One header; no in-pane section. */}
      <TopbarPortal side="start">
        <div aria-hidden className="absolute inset-y-0 border-l" style={{ left: paneW - 1 }} />
        <div
          className="absolute inset-y-0 left-0 flex items-center justify-end gap-1 pr-4"
          style={{ width: paneW }}
        >
          <Menu>
            <MenuTrigger
              render={
                <IconButton
                  variant={kind !== "all" ? "tertiary" : "ghost"}
                  size="sm"
                  icon={<Icon icon={ListFilter} />}
                  aria-label="Filter by kind"
                />
              }
            />
            <MenuContent align="start">
              <MenuRadioGroup value={kind} onValueChange={setKind}>
                <MenuRadioItem value="all" label="All notifications" />
                {Object.entries(KIND_META)
                  .filter(([k]) => kindsPresent.has(k))
                  .map(([k, meta]) => (
                    <MenuRadioItem
                      key={k}
                      value={k}
                      icon={<Icon icon={meta.icon} />}
                      label={meta.label}
                    />
                  ))}
              </MenuRadioGroup>
            </MenuContent>
          </Menu>

          <Menu>
            <MenuTrigger
              render={
                <IconButton
                  variant={unreadOnly ? "tertiary" : "ghost"}
                  size="sm"
                  icon={<Icon icon={SlidersHorizontal} />}
                  aria-label="Display options"
                />
              }
            />
            <MenuContent align="start">
              <MenuCheckboxItem
                checked={unreadOnly}
                onCheckedChange={(c) => setUnreadOnly(c === true)}
                label="Unread only"
                trailing={
                  unreadCount > 0 ? <span className="tabular-nums">{unreadCount}</span> : undefined
                }
              />
            </MenuContent>
          </Menu>

          <IconButton
            variant="ghost"
            size="sm"
            icon={<Icon icon={CheckCheck} />}
            aria-label="Mark all read"
            disabled={unreadCount === 0}
            onClick={() => void markAll()}
          />
        </div>
      </TopbarPortal>

      {/* the SELECTED notification's controls, top right of the strip (Linear's grammar) */}
      <TopbarPortal>
        {selected && openLabel(selected) && (
          <Button
            variant="ghost"
            size="sm"
            stretch="auto"
            icon={<Icon icon={ArrowRight} />}
            iconPosition="end"
            label={openLabel(selected) ?? ""}
            onClick={() => openTarget(selected)}
          />
        )}
      </TopbarPortal>

      <div className="flex shrink-0 flex-col border-r" style={{ width: paneW }}>
        <div className="scrollbar-thin px-gutter-pane min-h-0 flex-1 overflow-y-auto pt-4 pb-3">
          {visible.length === 0 ? (
            <EmptyState
              compact
              icon={InboxIcon}
              title={
                // "Unread only" with zero unread IS caught up - the happy message,
                // not a shrug; "Nothing here" is for kind filters that found nothing.
                kind === "all" && (!unreadOnly || unreadCount === 0)
                  ? "You're all caught up"
                  : "Nothing here"
              }
              className="mt-2"
            />
          ) : (
            <ul className="space-y-0.5">
              {visible.map((item) => {
                const Icon = metaOf(item)?.icon ?? InboxIcon;
                return (
                  <li key={item.id}>
                    <button
                      onClick={() => select(item)}
                      onDoubleClick={() => openTarget(item)}
                      aria-current={selectedId === item.id}
                      className={cn(
                        "hover:bg-accent/40 flex w-full items-start gap-2.5 rounded-md px-2.5 py-2 text-left transition-colors",
                        selectedId === item.id && "bg-accent/60",
                      )}
                    >
                      <Icon className="text-muted-foreground mt-0.5 size-4 shrink-0" />
                      <span className="min-w-0 flex-1">
                        <span className="flex items-baseline justify-between gap-2">
                          {/* The row names the TASK; the event ("Delivered: …") is the
                              second line. Items without a task keep title + body snippet. */}
                          <span
                            className={cn(
                              "truncate text-body-sm-regular",
                              !item.read && "font-medium",
                            )}
                          >
                            {item.task_title ?? item.title}
                          </span>
                          <span className="text-muted-foreground shrink-0 text-caption-md-regular">
                            {ago(item.created_at)}
                          </span>
                        </span>
                        <span className="text-muted-foreground block truncate text-caption-md-regular">
                          {item.task_title ? item.title : plain(item.body)}
                        </span>
                      </span>
                      <span
                        className={cn(
                          "mt-1.5 size-1.5 shrink-0 rounded-full",
                          item.read ? "bg-transparent" : "bg-info",
                        )}
                      />
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </div>

      {/* the slider: a grab strip over the divider - drag to resize, double-click resets,
          arrow keys when focused; the strip's divider and cluster follow the same width */}
      <div
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize notification list"
        aria-valuenow={paneW}
        aria-valuemin={PANE_MIN}
        aria-valuemax={PANE_MAX}
        tabIndex={0}
        onPointerDown={beginPaneResize}
        onPointerMove={paneResize}
        onPointerUp={endPaneResize}
        onPointerCancel={endPaneResize}
        onDoubleClick={() => commitPane(PANE_DEFAULT)}
        onKeyDown={(e) => {
          if (e.key === "ArrowLeft") commitPane(clampPane(paneW - 16));
          if (e.key === "ArrowRight") commitPane(clampPane(paneW + 16));
        }}
        className="hover:bg-ring/40 focus-visible:bg-ring/40 z-10 -ml-1 w-1.5 shrink-0 cursor-col-resize outline-hidden transition-colors"
      />

      <div className="scrollbar-thin min-w-0 flex-1 overflow-y-auto">
        {selected ? (
          // Keyed: a fresh notification is a fresh slate (no stale reader, no carried scroll).
          <NotificationDetail key={selected.id} item={selected} />
        ) : (
          <div className="flex h-full items-center justify-center">
            <EmptyState
              compact
              icon={InboxIcon}
              title="Select a notification"
              description="Answer questions and open deliverables without leaving the inbox."
            />
          </div>
        )}
      </div>
    </div>
  );
}

/** The right pane: what the notification is ABOUT, actionable in place - open questions are
 *  answerable here (the task resumes without leaving the inbox), deliverables open in the
 *  reader, the conversation replays underneath, and the task capsule closes the pane as the
 *  quiet receipt. Items without a task (shared projects, budget notes) show their body; the
 *  open-task/project action lives in the TOPBAR's right end (the selected item's controls,
 *  Linear's grammar). Keyed by item in the parent. */
function NotificationDetail({ item }: { item: Item }) {
  const [task, setTask] = useState<Awaited<ReturnType<typeof fetchTask>> | null>(null);
  const [view, setView] = useState<TaskView | null>(null);
  const [openArtifact, setOpenArtifact] = useState<string | null>(null);
  // Bumped when a gate resolves here: refetch so the answered gate leaves the pane and the
  // receipt's status moves on - without blanking into a loading flash.
  const [refreshKey, setRefreshKey] = useState(0);

  useEffect(() => {
    if (!item.task_id) return;
    let live = true;
    fetchTask(item.task_id)
      .then((t) => {
        if (live) setTask(t);
      })
      .catch(() => {});
    replayTaskEvents(item.task_id)
      .then((events) => {
        if (live) setView(foldEvents(events));
      })
      .catch(() => {});
    return () => {
      live = false;
    };
  }, [item.task_id, refreshKey]);

  const feed = view?.feed ?? [];
  const turns = feed.filter((f) => f.kind === "user" || f.kind === "segment");
  const deliverables = view ? deliveredArtifacts(view) : [];
  // A gate is only actionable while the task can still hear the answer - and offers only
  // what the server takes from THIS viewer (never `always` to anyone but the creator).
  const viewerId = useViewer()?.id;
  const gates = view && !TERMINAL.has(view.status) ? gatesFor(view, viewerId) : [];

  return (
    // Left-anchored against the list's border (Linear's detail grammar). The PANE is the
    // measure: the user sizes it with the slider, so no max-w cap - a cap left-anchors into
    // dead right space. pt-4 levels the detail's first line with the strip's first row; the
    // gutter-pane + 8px inset keeps prose off the divider, level with the list rows' text.
    <div className="w-full space-y-6 px-[calc(var(--spacing-gutter-pane)+0.5rem)] pt-4 pb-8">
      <div className="space-y-1">
        <p className="text-muted-foreground text-caption-md-regular">
          {metaOf(item)?.label ?? item.kind} · {ago(item.created_at)}
        </p>
        <h2 className="font-heading text-h4-regular font-semibold tracking-tight text-balance">
          {item.title}
        </h2>
        {item.body && <Markdown>{item.body}</Markdown>}
      </div>

      {item.task_id &&
        (view === null ? (
          <LoadingState />
        ) : (
          <div className="space-y-6">
            {/* What the machine is waiting on - answer here and it resumes. */}
            {gates.map((gate) => (
              <GateCard
                key={gate.gate_id}
                gate={controlGateFor(gate, task?.machine)}
                isCreator={task?.is_creator ?? false}
                onResolved={() => setRefreshKey((k) => k + 1)}
                budgetTask={
                  task
                    ? {
                        id: task.id,
                        projectId: task.project_id ?? null,
                        spentUsd: Number(view?.usageUsd ?? task.cost.total_usd ?? 0),
                      }
                    : undefined
                }
              />
            ))}
            {GATE_KINDS.has(item.kind) && gates.length === 0 && (
              <p className="text-muted-foreground text-body-sm-regular">{noQuestionsLine(view)}</p>
            )}

            {/* What it made - read the deliverable without leaving the inbox. */}
            {deliverables.length > 0 && (
              <div className="space-y-2">
                {deliverables.map((d) => (
                  <ArtifactCard
                    key={d.id}
                    title={d.title}
                    artifact={view.artifacts.find((a) => a.id === d.artifactId)}
                    onOpen={() => setOpenArtifact(d.artifactId)}
                  />
                ))}
              </div>
            )}

            {/* The chat itself, replayed read-only through the same fold the live page uses -
                a replayed transcript can never disagree with a streamed one. User turns wear
                the same right-aligned bubble as the task page (kit UserBubble). */}
            {turns.length > 0 && (
              <div className="space-y-5">
                <LabeledDivider>conversation</LabeledDivider>
                {turns.map((f) =>
                  f.kind === "user" ? (
                    <UserBubble key={f.id}>{f.text}</UserBubble>
                  ) : (
                    <Markdown key={f.id}>{f.markdown}</Markdown>
                  ),
                )}
              </div>
            )}

            {/* the receipt: where the task stands, demoted to the tail */}
            {task && (
              <div className="ring-foreground/10 bg-card space-y-3 rounded-lg p-4 ring-1 ring-inset">
                <div className="flex flex-wrap items-center gap-2">
                  <StatusBadge status={task.status} />
                  <span className="text-muted-foreground text-caption-md-regular">
                    <Money usd={task.cost.total_usd} /> · {ago(task.created_at)}
                  </span>
                </div>
                <p className="text-body-sm-regular font-medium">{task.title || task.goal}</p>
                {(task.plan.steps ?? []).length > 0 && (
                  <StepTimeline steps={task.plan.steps ?? []} />
                )}
              </div>
            )}
          </div>
        ))}

      <ArtifactReader
        artifactId={openArtifact}
        onClose={() => setOpenArtifact(null)}
        kindHint={view?.artifacts.find((a) => a.id === openArtifact)?.type}
        // A save/restore in the drawer mints a new head - repoint the open id so
        // kindHint and a reopen track the lineage's face, same as the task page hosts.
        onArtifactChanged={setOpenArtifact}
      />
    </div>
  );
}

const fetchTask = (id: string) =>
  unwrap(api.GET("/v1/tasks/{task_id}", { params: { path: { task_id: id } } }));

// A failed loader (unwrap throws) shows the friendly boundary in the content area with the
// shell intact, not a whole-app crash - same pattern as the task/project detail routes.
export { RouteErrorBoundary as ErrorBoundary } from "~/components/layout/route-error";
