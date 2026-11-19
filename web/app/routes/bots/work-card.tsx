/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The work card: one surface per task in the bot's chat thread. It fills in while the task
// runs (the task's own two-lane stream, mounted only while live), asks its gates in place, and
// ends holding the deliverable, with Reply (continue this work) and Details (the task page).

import { Check, ChevronRight, Circle } from "lucide-react";
import { type ReactNode, useState } from "react";
import { Link } from "react-router";
import { Liveness, artifactIcon } from "~/components/kit";
import { GateDock } from "~/components/tasks/gate-dock";
import { useLoad } from "~/hooks/use-load";
import { useSeenTask } from "~/hooks/use-seen-task";
import { useTaskStream } from "~/hooks/use-task-stream";
import { api, unwrap } from "~/lib/api/client";
import type { components } from "~/lib/api/types.gen";
import { cardSettle } from "~/lib/chat-stream";
import { taskHref, type TaskRef } from "~/lib/links";
import { cardSeenKey } from "~/lib/seen-task";
import { type GateView, type TaskView, initialView } from "~/lib/task-view";
import { formatSeconds } from "~/lib/time";
import { cn } from "~/lib/utils";
import { toGateView } from "./gate-view";

type MessageOut = components["schemas"]["MessageOut"];
type GateOut = components["schemas"]["GateOut"];
type TaskDetailOut = components["schemas"]["TaskDetailOut"];

export function WorkCard({
  taskId,
  projectId,
  rows,
  botSeq,
  name,
  gates,
  onOpenArtifact,
  onReply,
  onGateResolved,
}: {
  taskId: string;
  /** The conversation's Project - the Details link's fallback until the task itself is read. */
  projectId: string | null;
  rows: MessageOut[];
  /** The newest bot line that came home for this task (a continued turn's answer). */
  botSeq: number | undefined;
  name: string;
  gates: Record<string, GateOut>;
  onOpenArtifact: (id: string) => void;
  onReply: (taskId: string, label: string) => void;
  onGateResolved: () => void;
}) {
  // The LAST turn decides (a reply under a delivered card starts a new turn, and the card is
  // live again until that turn comes home - as a settle row, or as the bot's own line).
  const { settled, row: settledRow } = cardSettle(rows, botSeq);
  const lastFailed = [...rows].reverse().find((r) => r.kind === "failed");
  // Gates answered from this card: the docks keep no state of their own, resolution appends
  // no conversation row, and the open-gates list is a poll - so the card remembers.
  const [resolved, setResolved] = useState<ReadonlySet<string>>(() => new Set());
  // Watching the card is watching the task: news on it (a gate, a settle, the bot's line) reads
  // the viewer's inbox items about the task, before their phone's alert goes out.
  useSeenTask(taskId, cardSeenKey(rows, botSeq), { onOpen: false });

  // The task itself - its title and plan. Re-read when the card settles, so the finished card
  // shows the steps as they ended; while live, the task's own stream keeps it current.
  const { data: detail = null, error: detailError } = useLoad(
    (signal) =>
      unwrap(api.GET("/v1/tasks/{task_id}", { params: { path: { task_id: taskId } }, signal })),
    [taskId, settled, settledRow?.seq],
  );
  const detailFailed = detailError !== undefined;

  const started = rows.find((r) => r.kind === "started");
  const title = detail?.title ?? started?.text.replace(/^Started a task: /, "") ?? "Working";
  const gateRows = rows.filter(
    (r) => r.kind === "gate" && r.gate_id && gates[r.gate_id] && !resolved.has(r.gate_id),
  );
  const markResolved = (gateId: string) => {
    setResolved((s) => new Set([...s, gateId]));
    onGateResolved();
  };

  const shell = (body: ReactNode, status: ReactNode) => (
    <CardShell
      name={name}
      title={title}
      status={status}
      gates={gateRows.map((g) => ({
        key: g.seq,
        gate: toGateView(gates[g.gate_id!]!),
        onResolved: () => markResolved(g.gate_id!),
      }))}
      outcome={outcomeOf(settled, settledRow, lastFailed, started)}
      artifactIds={settledRow?.artifact_ids ?? []}
      onOpenArtifact={onOpenArtifact}
      onReply={() => onReply(taskId, title)}
      details={{ id: taskId, project_id: detail?.project_id ?? projectId }}
    >
      {body}
    </CardShell>
  );

  if (settled) {
    // Settled first: a card whose task could not be read still says how it ended.
    return shell(
      detail ? (
        <Steps view={initialView(detail)} />
      ) : detailFailed ? (
        <p className="text-muted-foreground text-caption-md-regular">Couldn't load the task.</p>
      ) : null,
      <SettledBadge kind={settledRow?.kind ?? "delivered"} />,
    );
  }
  if (!detail) {
    return shell(
      detailFailed ? (
        <p className="text-muted-foreground text-caption-md-regular">Couldn't load the task.</p>
      ) : null,
      detailFailed ? (
        <span className="text-muted-foreground text-caption-md-regular">unknown</span>
      ) : (
        <span className="text-muted-foreground flex items-center gap-1.5 text-caption-md-regular">
          <Liveness className="size-3.5" /> starting
        </span>
      ),
    );
  }
  return <LiveCard detail={detail} shell={shell} />;
}

function SettledBadge({ kind }: { kind: string }) {
  if (kind === "failed")
    return <span className="text-destructive text-caption-md-regular">failed</span>;
  if (kind === "cancelled")
    return <span className="text-muted-foreground text-caption-md-regular">cancelled</span>;
  return (
    <span className="text-muted-foreground flex items-center gap-1.5 text-caption-md-regular">
      <Check className="text-success size-3.5" /> done
    </span>
  );
}

/** The live half: the task's own two-lane stream, mounted only while the card is running. */
function LiveCard({
  detail,
  shell,
}: {
  detail: TaskDetailOut;
  shell: (body: ReactNode, status: ReactNode) => ReactNode;
}) {
  const { view } = useTaskStream(detail.id, detail);
  const phase = view.live?.phase ?? "working";
  const elapsed = view.live?.elapsed_s ?? 0;
  // The task's own stream knows when it stopped (a cancel never writes a card row of its
  // own turn while the workflow is parked): a terminal status settles the card here too.
  if (view.status === "cancelled" || view.status === "failed" || view.status === "delivered") {
    return (
      <>
        {shell(
          <Steps view={view} />,
          <SettledBadge kind={view.status === "delivered" ? "delivered" : view.status} />,
        )}
      </>
    );
  }
  return (
    <>
      {shell(
        <Steps view={view} />,
        <span className="text-muted-foreground flex items-center gap-1.5 text-caption-md-regular">
          <Liveness className="size-3.5" /> {phase}
          {elapsed > 0 && <span className="tabular-nums"> · {formatSeconds(elapsed)}</span>}
        </span>,
      )}
    </>
  );
}

const DONE_STATES = new Set(["done", "skipped"]);
const LIVE_STATES = new Set(["running", "verifying", "waiting"]);

function Steps({ view }: { view: TaskView }) {
  if (!view.steps.length) return null;
  return (
    <div className="flex flex-col gap-1">
      {view.steps.map((s) => {
        const state: string = s.state; // the wire's vocabulary; the card only needs three moods
        return (
          <div
            key={s.id}
            className={cn(
              "flex items-center gap-2 text-caption-md-regular",
              DONE_STATES.has(state) || LIVE_STATES.has(state)
                ? "text-foreground"
                : "text-muted-foreground",
            )}
          >
            {DONE_STATES.has(state) ? (
              <Check className="text-success size-3.5 shrink-0" />
            ) : LIVE_STATES.has(state) ? (
              <Liveness className="size-3.5" />
            ) : state === "failed" ? (
              <Circle className="text-destructive size-3.5 shrink-0" />
            ) : (
              <Circle className="text-muted-foreground size-3.5 shrink-0" />
            )}
            <span className="truncate">{s.title}</span>
            {s.agent && <span className="text-muted-foreground shrink-0">{s.agent}</span>}
          </div>
        );
      })}
    </div>
  );
}

export function ArtifactChip({ id, onOpen }: { id: string; onOpen: (id: string) => void }) {
  const Icon = artifactIcon("document");
  return (
    <button
      type="button"
      onClick={() => onOpen(id)}
      className="bg-card hover:bg-muted flex items-center gap-1.5 rounded-md border px-2 py-1 text-caption-md-regular transition-colors"
    >
      <Icon className="size-3.5" /> Open deliverable
    </button>
  );
}

export function DetailsLink({ task }: { task: TaskRef }) {
  return (
    <Link
      to={taskHref(task)}
      className="text-muted-foreground hover:text-foreground flex items-center gap-0.5 text-caption-md-regular transition-colors"
    >
      Details <ChevronRight className="size-3" />
    </Link>
  );
}

/** How the card's turn ended, as one line under its body: a failed or cancelled settle says
 *  so, and a live turn shows a failure that came after it started. */
type Outcome = { tone: "destructive" | "muted"; text: string } | null;

function outcomeOf(
  settled: boolean,
  settledRow: MessageOut | undefined | null,
  lastFailed: MessageOut | undefined,
  started: MessageOut | undefined,
): Outcome {
  if (settled && settledRow?.kind === "failed")
    return { tone: "destructive", text: settledRow.detail || settledRow.text };
  if (settled && settledRow?.kind === "cancelled")
    return { tone: "muted", text: settledRow.detail || settledRow.text };
  if (!settled && lastFailed && lastFailed.seq > (started?.seq ?? 0))
    return { tone: "destructive", text: lastFailed.detail || lastFailed.text };
  return null;
}

/** The card's frame: the bot's name, the title and status, the body, the gates it asks in
 *  place, how the turn ended, and the footer (the deliverable, Reply, Details). */
function CardShell({
  name,
  title,
  status,
  children,
  gates,
  outcome,
  artifactIds,
  onOpenArtifact,
  onReply,
  details,
}: {
  name: string;
  title: string;
  status: ReactNode;
  children: ReactNode;
  gates: { key: number; gate: GateView; onResolved: () => void }[];
  outcome: Outcome;
  artifactIds: string[];
  onOpenArtifact: (id: string) => void;
  onReply: () => void;
  details: TaskRef;
}) {
  return (
    <div className="flex max-w-[92%] flex-col gap-2">
      <div className="text-muted-foreground text-caption-md-regular">{name}</div>
      <div className="bg-card flex w-[560px] max-w-full flex-col gap-2.5 rounded-lg border p-3 text-body-sm-regular">
        <div className="flex items-center justify-between gap-2">
          <span className="font-medium">{title}</span>
          {status}
        </div>
        {children}
        {gates.map((g) => (
          <GateDock key={g.key} gate={g.gate} onResolved={g.onResolved} />
        ))}
        {outcome && (
          <p
            className={cn(
              outcome.tone === "destructive" ? "text-destructive" : "text-muted-foreground",
              "text-caption-md-regular",
            )}
          >
            {outcome.text}
          </p>
        )}
        <div className="flex items-center justify-between gap-2 border-t pt-2">
          <div className="flex flex-wrap items-center gap-1.5">
            {artifactIds.map((id) => (
              <ArtifactChip key={id} id={id} onOpen={onOpenArtifact} />
            ))}
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              className="text-muted-foreground hover:text-foreground text-caption-md-regular underline-offset-2 hover:underline"
              onClick={onReply}
            >
              Reply
            </button>
            <DetailsLink task={details} />
          </div>
        </div>
      </div>
    </div>
  );
}
