/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// /routines - the standing-work manager (ADR-048). A contained LIST page per the DESIGN
// list-page checklist: no in-page title (the crumb is the name), controls ride the topbar,
// keyset rows. Creation lives HERE (the /projects CreateProject grammar):
// "+ New routine" opens the dialog on this screen; the old hand-off to the home composer's
// When dial read as the app jumping away. The composer's dial remains the organic lane -
// a send you decide to make standing. The detail is a PEEK, URL-borne (`?routine=`), and a
// run opens the task thread IN the same sheet with a ← back - the bots-peek swap, nothing
// navigates away.

import { AlarmClock, ArrowLeft, EllipsisVertical, Pause, Play, Zap } from "lucide-react";
import { useEffect, useState } from "react";
import { useSearchParams } from "react-router";
import { revalidateOnPageChange } from "~/lib/pending-route";
import { toast } from "@prismoid/blocks/toast";
import {
  EmptyState,
  LoadMore,
  LoadingState,
  Markdown,
  MetaChip,
  Money,
  PageHeader,
  Pill,
  Rows,
  Section,
} from "~/components/kit";
import { PeekSheet } from "~/components/artifact/artifact-reader";
import { SectionTab } from "~/components/layout/section-tab";
import { TopbarPortal } from "~/components/layout/topbar-slot";
import { TaskPeekContent } from "~/components/tasks/task-peek";
import { Button } from "@makeplane/propel/components/button";
import { Icon } from "@makeplane/propel/components/icon";
import { IconButton } from "@makeplane/propel/components/icon-button";
import { DrawerTitle } from "@makeplane/propel/components/drawer";
import { Menu, MenuContent, MenuItem, MenuTrigger } from "@makeplane/propel/components/menu";
import { api, toastError, unwrap } from "~/lib/api/client";
import { ago, formatDateTime, inAgo } from "~/lib/format";
import { useKeysetList } from "~/lib/paging";
import type { components } from "~/lib/api/types.gen";
import type { Route } from "./+types/route";
import { collapseRuns, decisionLabel, type RunRow } from "./streaks";
import { fireNote } from "./fire-note";
import { NewRoutine } from "./new-routine";

type RoutineRow = components["schemas"]["RoutineOut"];

export function meta() {
  return [{ title: "Routines · Prismoid" }];
}

const PAGE = 40;

export async function clientLoader() {
  const page = await unwrap(api.GET("/v1/routines", { params: { query: { limit: PAGE } } }));
  return { items: page.items, cursor: page.next_cursor ?? null };
}
clientLoader.hydrate = true as const;

// A tab, a filter or an open peek rides the query string: the page already holds its data, so
// the router does not hold that click for another fetch.
export const shouldRevalidate = revalidateOnPageChange;

const STATUS_TONE = { active: "success", paused: "warning", retired: "muted" } as const;

/** The When, as ONE sentence - the no-builder law: copy always says when, never a diagram. */
export function whenSentence(r: RoutineRow): string {
  const s = r.schedule as {
    cron?: string;
    every_s?: number;
    at?: string;
    timezone?: string;
  } | null;
  if (s?.cron)
    return `cron ${s.cron}${s.timezone && s.timezone !== "UTC" ? ` · ${s.timezone}` : ""}`;
  if (s?.every_s) {
    const m = s.every_s / 60;
    return m >= 60 ? `every ${m / 60}h` : `every ${m}m`;
  }
  if (s?.at) return `once, ${formatDateTime(s.at)}`;
  const ev = r.events as { kinds?: string[] } | null;
  if (ev?.kinds?.length) return `when ${ev.kinds.join(" or ")}`;
  return r.trigger_kind;
}

export default function Routines({ loaderData }: Route.ComponentProps) {
  // Peek verbs (pause/retire/fire) change rows the list already holds - bumping this dep
  // resets the keyset list to a fresh page one (the hook has reset-on-deps, not refresh()).
  const [bump, setBump] = useState(0);
  const [searchParams, setSearchParams] = useSearchParams();
  const openId = searchParams.get("routine");
  const setOpenId = (id: string | null) =>
    setSearchParams(
      (p) => {
        const n = new URLSearchParams(p);
        if (id) n.set("routine", id);
        else {
          n.delete("routine");
          n.delete("run");
        }
        return n;
      },
      { replace: id === null },
    );

  const list = useKeysetList<RoutineRow>({
    fetchPage: (cursor) =>
      unwrap(
        api.GET("/v1/routines", {
          params: { query: { limit: PAGE, ...(cursor ? { cursor } : {}) } },
        }),
      ),
    deps: [bump],
    orderKey: (r) => r.created_at,
    initial: bump === 0 ? { items: loaderData.items, next_cursor: loaderData.cursor } : undefined,
  });
  const items = list.items ?? [];

  return (
    <SectionTab>
      {/* No search box: the wire has no q param, and this page never fakes a control.
          One primary action, and it opens ON this screen. */}
      <TopbarPortal>
        <NewRoutine
          onCreated={(id) => {
            setBump((b) => b + 1);
            setOpenId(id);
          }}
        />
      </TopbarPortal>

      {items.length === 0 ? (
        <EmptyState
          icon={AlarmClock}
          title="No routines yet"
          description="A routine is a task you save instead of send. + New routine sets one up. Or: prismoid routines create “…” --cron “0 9 * * 1-5”"
        />
      ) : (
        <>
          <Rows>
            {items.map((r) => (
              <li key={r.id}>
                <button
                  type="button"
                  onClick={() => setOpenId(r.id)}
                  className="hover:bg-accent/50 flex w-full items-center gap-3 px-4 py-3 text-left transition-colors"
                >
                  <AlarmClock className="text-muted-foreground size-4 shrink-0" />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-body-sm-regular font-medium">
                      {r.name}
                    </span>
                    <span className="text-muted-foreground block truncate text-caption-md-regular">
                      {whenSentence(r)}
                      {r.keeper !== "one" ? ` · kept by ${r.keeper}` : ""}
                    </span>
                  </span>
                  {r.consecutive_failures > 0 && r.status === "active" && (
                    <Pill tone="warning">{r.consecutive_failures} failing</Pill>
                  )}
                  {r.next_fire_at && r.status === "active" && (
                    <span className="text-muted-foreground text-caption-md-regular tabular-nums">
                      {inAgo(r.next_fire_at)}
                    </span>
                  )}
                  <Pill tone={STATUS_TONE[r.status as keyof typeof STATUS_TONE] ?? "muted"}>
                    {r.status}
                  </Pill>
                </button>
              </li>
            ))}
          </Rows>
          <LoadMore hasMore={list.hasMore} busy={list.loadingMore} onClick={list.loadMore} />
        </>
      )}

      <PeekSheet open={openId !== null} onOpenChange={(o) => !o && setOpenId(null)}>
        <div className="sr-only">
          <DrawerTitle>Routine</DrawerTitle>
        </div>
        {openId !== null && (
          <RoutinePeek
            key={openId}
            routineId={openId}
            row={items.find((r) => r.id === openId)}
            onClose={() => setOpenId(null)}
            onChanged={() => setBump((b) => b + 1)}
          />
        )}
      </PeekSheet>
    </SectionTab>
  );
}

// ── the detail peek ───────────────────────────────────────────────────────────────────────────

function RoutinePeek({
  routineId,
  row,
  onClose,
  onChanged,
}: {
  routineId: string;
  /** The list's own row for this routine, when it holds one: the peek opens on it at once. */
  row?: RoutineRow;
  onClose: () => void;
  onChanged?: () => void;
}) {
  const [searchParams, setSearchParams] = useSearchParams();
  const runTask = searchParams.get("run");
  const setRunTask = (id: string | null) =>
    setSearchParams(
      (p) => {
        const n = new URLSearchParams(p);
        if (id) n.set("run", id);
        else n.delete("run");
        return n;
      },
      { replace: id === null },
    );

  // The list row and the detail are one shape, so the peek draws from the row on the click and
  // the fetch below only freshens it. A deep link to a routine the list has not loaded waits.
  const [detail, setDetail] = useState<RoutineRow | null>(row ?? null);
  const [busy, setBusy] = useState(false);
  // Run-now promises "the run shows below" - bumping this re-runs the ledger's keyset query,
  // which otherwise never refetches while the peek stays open.
  const [runsBump, setRunsBump] = useState(0);
  const reload = () => {
    void unwrap(
      api.GET("/v1/routines/{routine_id}", { params: { path: { routine_id: routineId } } }),
    ).then(setDetail, (e) => toastError(e, "Couldn't open this routine."));
  };
  useEffect(reload, [routineId]);

  // A run swaps the pane to the task thread - ← returns to the routine (the bots-peek grammar).
  if (runTask !== null) {
    return (
      <>
        <div className="flex items-center gap-1 border-b px-3 py-2">
          <IconButton
            variant="ghost"
            size="sm"
            icon={<Icon icon={ArrowLeft} />}
            aria-label="Back to the routine"
            onClick={() => setRunTask(null)}
          />
          <span className="text-muted-foreground truncate text-caption-md-regular">
            {detail?.name ?? "Routine"} · run
          </span>
        </div>
        <TaskPeekContent key={runTask} taskId={runTask} onClose={() => setRunTask(null)} />
      </>
    );
  }

  if (detail === null) return <LoadingState label="Opening the routine…" />;
  const r = detail;

  /** Call a door, then say so: a fixed line, or one read off what the door answered. */
  const act = async <T,>(fn: () => Promise<T>, done: string | ((answer: T) => void)) => {
    setBusy(true);
    try {
      const answer = await fn();
      if (typeof done === "string") toast.success(done);
      else done(answer);
      reload();
      onChanged?.();
    } catch (e) {
      toastError(e, "That didn't land.");
    } finally {
      setBusy(false);
    }
  };
  const path = { params: { path: { routine_id: r.id } } };

  return (
    <div className="scrollbar-thin min-h-0 flex-1 space-y-6 overflow-y-auto px-6 py-5">
      <PageHeader
        as="h2"
        title={r.name}
        actions={
          <>
            {r.status !== "retired" && (
              <Button
                variant="secondary"
                size="sm"
                stretch="auto"
                icon={<Icon icon={Zap} />}
                label="Run now"
                disabled={busy}
                onClick={() =>
                  act(
                    () => unwrap(api.POST("/v1/routines/{routine_id}/fire", path)),
                    (fired) => {
                      const note = fireNote(fired);
                      ({ success: toast.success, info: toast.info, error: toast.error })[note.tone](
                        note.text,
                      );
                    },
                  ).then(() => setRunsBump((b) => b + 1))
                }
              />
            )}
            {r.status === "active" && (
              <Button
                variant="ghost"
                size="sm"
                stretch="auto"
                icon={<Icon icon={Pause} />}
                label="Pause"
                disabled={busy}
                onClick={() =>
                  act(
                    () =>
                      unwrap(api.POST("/v1/routines/{routine_id}/pause", { ...path, body: {} })),
                    "Paused.",
                  )
                }
              />
            )}
            {r.status === "paused" && (
              <Button
                variant="ghost"
                size="sm"
                stretch="auto"
                icon={<Icon icon={Play} />}
                label="Resume"
                disabled={busy}
                onClick={() =>
                  act(
                    () => unwrap(api.POST("/v1/routines/{routine_id}/resume", path)),
                    "Resumed - next fire recomputed.",
                  )
                }
              />
            )}
            <RoutineMenu r={r} busy={busy} act={act} onClose={onClose} />
          </>
        }
      />

      <div className="space-y-2">
        <p className="text-body-sm-regular">
          <span className="text-muted-foreground">When: </span>
          {whenSentence(r)}
          {r.posture === "hold" && (
            <span className="text-muted-foreground"> · runs wait for your release</span>
          )}
        </p>
        {r.status === "paused" && r.pause_note && (
          <p className="text-warning-foreground bg-warning/10 rounded-lg px-3 py-2 text-caption-md-regular">
            Paused ({r.paused_reason}): {r.pause_note}
          </p>
        )}
        {r.upcoming.length > 0 && (
          <p className="text-muted-foreground text-caption-md-regular tabular-nums">
            next {r.upcoming.map((t) => formatDateTime(t)).join(" · ")}
          </p>
        )}
        <div className="flex flex-wrap gap-1.5">
          <MetaChip>
            <Money usd={r.budget_usd_per_run} /> / run
          </MetaChip>
          <MetaChip>
            spent <Money usd={r.spend_mtd_usd ?? 0} /> this month
          </MetaChip>
          <MetaChip>overlap {r.overlap}</MetaChip>
          <MetaChip>catch-up {r.catch_up}</MetaChip>
          <MetaChip>notify {r.notify}</MetaChip>
          {r.max_runs != null && (
            <MetaChip>
              {r.runs_minted}/{r.max_runs} runs
            </MetaChip>
          )}
        </div>
      </div>

      <Section title="Instruction">
        <div className="text-body-sm-regular">
          <Markdown>{r.instruction}</Markdown>
        </div>
      </Section>

      <RunsLedger routineId={r.id} refresh={runsBump} onOpenTask={setRunTask} />
    </div>
  );
}

function RoutineMenu({
  r,
  busy,
  act,
  onClose,
}: {
  r: RoutineRow;
  busy: boolean;
  act: (fn: () => Promise<unknown>, done: string) => Promise<void>;
  onClose: () => void;
}) {
  const path = { params: { path: { routine_id: r.id } } };
  return (
    <Menu>
      <MenuTrigger
        render={
          <IconButton
            variant="ghost"
            size="sm"
            icon={<Icon icon={EllipsisVertical} />}
            aria-label="More"
            disabled={busy}
          />
        }
      />
      <MenuContent align="end">
        <MenuItem
          label="Mint fire token…"
          onClick={() =>
            void unwrap(api.POST("/v1/routines/{routine_id}/token", path)).then(
              (out) => {
                // Shown ONCE (the ck_ ceremony) - the copy dialog is the clipboard.
                void navigator.clipboard?.writeText(out.token);
                toast.success("Fire token copied. Shown once; minting again rotates it.", {
                  description: out.token,
                  duration: 15000,
                });
              },
              (e) => toastError(e, "Couldn't mint the token."),
            )
          }
        />
        <MenuItem
          label="Revoke fire token"
          onClick={() =>
            void act(
              () => unwrap(api.DELETE("/v1/routines/{routine_id}/token", path)),
              "Fire door closed.",
            )
          }
        />
        {r.status !== "retired" && (
          <MenuItem
            label="Retire routine"
            variant="danger"
            onClick={() => {
              // Retire keeps the ledger readable forever - the confirm is the menu itself
              // plus this named toast; a held dialog would restate the same sentence.
              void act(
                () => unwrap(api.POST("/v1/routines/{routine_id}/retire", path)),
                "Retired - the run history stays.",
              ).then(onClose);
            }}
          />
        )}
      </MenuContent>
    </Menu>
  );
}

// ── the runs ledger ───────────────────────────────────────────────────────────────────────────

const DECISION_TONE: Record<string, "success" | "warning" | "danger" | "muted" | "info"> = {
  minted: "success",
  held: "info",
  error: "danger",
  suppressed_loop: "danger",
  skipped_budget: "warning",
  skipped_allowance: "warning",
};

function RunsLedger({
  routineId,
  refresh,
  onOpenTask,
}: {
  routineId: string;
  refresh: number;
  onOpenTask: (taskId: string) => void;
}) {
  const list = useKeysetList<RunRow>({
    fetchPage: (cursor) =>
      unwrap(
        api.GET("/v1/routines/{routine_id}/runs", {
          params: {
            path: { routine_id: routineId },
            query: { limit: 30, ...(cursor ? { cursor } : {}) },
          },
        }),
      ),
    deps: [routineId, refresh],
    orderKey: (r) => r.at,
  });
  const rows = list.items ?? [];
  const ledger = collapseRuns(rows);

  return (
    <Section title="Runs">
      {rows.length === 0 && !list.loadingMore ? (
        <p className="text-muted-foreground text-body-sm-regular">
          No fires yet - the ledger starts with the first one.
        </p>
      ) : (
        <>
          <Rows>
            {ledger.map((item) =>
              item.kind === "streak" ? (
                <li
                  key={`streak-${item.latest.id}`}
                  className="text-muted-foreground flex items-center gap-3 px-4 py-2 text-caption-md-regular"
                >
                  <span className="bg-border h-px w-4" aria-hidden />
                  {item.count} quiet fire{item.count === 1 ? "" : "s"} (
                  {item.decisions.map(decisionLabel).join(", ")}), latest {ago(item.latest.at)}
                </li>
              ) : (
                <li key={item.run.id}>
                  <RunLine run={item.run} onOpenTask={onOpenTask} />
                </li>
              ),
            )}
          </Rows>
          <LoadMore hasMore={list.hasMore} busy={list.loadingMore} onClick={list.loadMore} />
        </>
      )}
    </Section>
  );
}

function RunLine({ run, onOpenTask }: { run: RunRow; onOpenTask: (taskId: string) => void }) {
  const body = (
    <>
      <Pill tone={DECISION_TONE[run.decision] ?? "muted"}>{decisionLabel(run.decision)}</Pill>
      <span className="min-w-0 flex-1">
        <span className="text-muted-foreground block truncate text-caption-md-regular">
          {run.source} · {ago(run.at)}
          {run.task_status ? ` · task ${run.task_status}` : ""}
          {run.detail ? ` · ${run.detail}` : ""}
        </span>
      </span>
    </>
  );
  if (run.task_id) {
    return (
      <button
        type="button"
        onClick={() => onOpenTask(run.task_id as string)}
        className="hover:bg-accent/50 flex w-full items-center gap-3 px-4 py-2.5 text-left transition-colors"
      >
        {body}
      </button>
    );
  }
  return <span className="flex items-center gap-3 px-4 py-2.5">{body}</span>;
}
