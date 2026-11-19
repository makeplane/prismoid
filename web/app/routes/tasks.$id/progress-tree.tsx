/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/**
 * ProgressTree - the execution story as a living, nested, collapsible tree (the plan's
 * flagship "no more waiting blind" surface).
 *
 * ## The grammar
 *   One `execution` FeedItem per turn renders as ONE of these. Settled → a single quiet
 *   "Completed 7 steps ▸" line that expands to the full story. Live → the root is open,
 *   the most-recently-active group is open, finished groups fold to their one-line
 *   summaries. User clicks always win over the derived defaults (override map, never
 *   persisted). Inside a group: the plan's checklist ticking, tool rows with verbs +
 *   durations, model-round rows ("Thinking… → Thought for Ns"), retry notes. Rows past
 *   MAX_VISIBLE_ROWS clamp behind "+N more" - the reducer keeps everything; only the
 *   render clamps.
 *
 * ## The laws it obeys
 *   text-body-sm-regular/text-caption-md-regular only · tone rides icons, text stays muted unless warning/danger ·
 *   the indent rail is the sanctioned hand-border (`border-l border-border` + `pl-4`
 *   here; StepTimeline draws the same rail as a `w-px bg-border` strut) · liveness =
 *   StatusDot ping / the Liveness fade trio / shimmer / the live elapsed clock (LiveTicker's
 *   precedent), nothing else animates - and NOTHING claims liveness once the item is
 *   settled · collapse = chevron transform only, never height · all copy comes from
 *   progress-copy.ts (new tool kinds are data rows, not components).
 */

// Base UI's Collapsible, not Propel's: see kit/disclosure.tsx.
import { Collapsible } from "@base-ui/react/collapsible";
import { AlertTriangle, Check, Circle, Minus } from "lucide-react";
import { memo, useCallback, useState } from "react";
import { Liveness, DisclosureChevron, StatusDot } from "~/components/kit";
import { Badge } from "@makeplane/propel/components/badge";
import { cn } from "~/lib/utils";
import type { ExecGroup, ExecRow, ExecutionItem } from "~/lib/task-view";
import { formatDurationMs } from "~/lib/time";
import { modelsOf } from "~/lib/usage-ledger";
import {
  agentHueIndex,
  ACTIVE_STEP_STATES,
  turnDuration,
  turnTokens,
  TEXT_COL_PAD,
  actorLabel,
  groupDuration,
  groupLedger,
  headline,
  isHeld,
  outcomeLabel,
  PLAN_ICON,
  ROUND_ICON,
  roundLine,
  rootLabel,
  toolIcon,
  toolLine,
} from "./progress-copy";
// The tail grammar and its ticking clock live in ONE module now - the
// pre-tree WorkingLine renders the same shape from the same parts.
import { useLiveElapsed } from "./tail-line";

const MAX_VISIBLE_ROWS = 6;

/** The agent chip's identity hue (nameHue's index law) on Propel's nearest label palette,
 *  in the hue-1..8 ramp's order (295° purple … 345° crimson). */
const AGENT_BADGE = [
  "purple",
  "indigo",
  "info",
  "emerald",
  "success",
  "yellow",
  "orange",
  "crimson",
] as const;

/** The most-recently-active group of a live item - the ONE that auto-opens. With ten
 *  parallel fleet seats, the others' closed headers still breathe (StatusDot ping),
 *  which IS the "and N other tasks" grammar without extra machinery. Before any step is
 *  active - intake, planning - the PLAN group is the show: its phase rows (Clarifier ·
 *  Reviewing your request…) are the only thing happening, so it opens. */
function latestActiveId(item: ExecutionItem): string | null {
  for (let i = item.groups.length - 1; i >= 0; i--) {
    if (item.groups[i].kind === "step" && ACTIVE_STEP_STATES.has(item.groups[i].state))
      return item.groups[i].id;
  }
  return item.groups.some((g) => g.id === "plan") ? "plan" : null;
}

/** Which groups the task's wait parks: a step holding an open gate. Parallel siblings keep
 *  working while one step asks, so only the asking step reads "waiting". A gate with no step
 *  (the plan's) parks every active step - nothing runs past it. */
export function parkedOn(item: ExecutionItem, status?: string): (groupId: string) => boolean {
  if (status !== "waiting") return () => false;
  const asking = new Set(
    item.groups
      .filter(
        (g) =>
          g.kind === "step" && g.rows.some((r) => r.t === "tool" && r.tool === "gate" && r.pending),
      )
      .map((g) => g.id),
  );
  return (id) => asking.size === 0 || asking.has(id);
}

export const ProgressTree = memo(function ProgressTree({
  item,
  status,
}: {
  item: ExecutionItem;
  /** The task's status - kept for the settled root line's vocabulary. */
  status?: string;
}) {
  // User overrides win over derived defaults; component-local, never persisted.
  const [rootOverride, setRootOverride] = useState<boolean | null>(null);
  const [overrides, setOverrides] = useState<Record<string, boolean>>({});
  // ONE stable callback for every group row - an inline lambda here remakes itself per
  // frame and defeats StepGroupRow's memo (every header re-rendering on every delta).
  const setGroupOpen = useCallback(
    (id: string, open: boolean) => setOverrides((m) => ({ ...m, [id]: open })),
    [],
  );
  const rootOpen = rootOverride ?? !item.settled;
  const activeId = item.settled ? null : latestActiveId(item);
  const parked = parkedOn(item, status);

  const groupRows = (
    <>
      {item.groups.map((group) => (
        <StepGroupRow
          key={group.id}
          group={group}
          settled={item.settled}
          parked={parked(group.id)}
          open={overrides[group.id] ?? group.id === activeId}
          onOpenChange={setGroupOpen}
        />
      ))}
    </>
  );

  if (item.settled) {
    // The receipt survives settlement (dropping it lost information):
    // the live tail's time · tokens migrate into the settled one-liner, per turn, from
    // the same frozen anchors the group rows keep.
    const receipt = [turnDuration(item), turnTokens(item)].filter(Boolean).join(" · ");
    return (
      <Collapsible.Root open={rootOpen} onOpenChange={(o) => setRootOverride(o)}>
        <Collapsible.Trigger className="text-muted-foreground hover:text-foreground group flex w-full items-center gap-2 text-left text-body-sm-regular transition-colors">
          <span className="min-w-0 truncate">{rootLabel(item, status)}</span>
          {receipt && (
            <span className="text-muted-foreground/70 shrink-0 text-caption-md-regular tabular-nums">
              {receipt}
            </span>
          )}
          <DisclosureChevron />
        </Collapsible.Trigger>
        <Collapsible.Panel className="space-y-1.5 pt-2.5">{groupRows}</Collapsible.Panel>
      </Collapsible.Root>
    );
  }

  // LIVE: groups only - the tail line lifted to ROUTE level (round five, owner: the
  // streaming artifact card renders below the tree, and the breathing line was stranded
  // above it). ExecTail (tail-line.tsx) is now the transcript's last element, mounted
  // after the draft card; this component's live face is purely the story.
  return <div className="space-y-1.5">{groupRows}</div>;
});

/** The row's leading mark: the plan's icon, or a step's state dot. */
function GroupMark({
  group,
  settled,
  parked,
  activeNow,
}: {
  group: ExecGroup;
  settled: boolean;
  parked: boolean;
  activeNow: boolean;
}) {
  const PlanIcon = PLAN_ICON;
  if (group.kind === "plan") return <PlanIcon className="text-muted-foreground size-3.5" />;
  // NOT decorative: this dot is the trigger's only carrier of step state for a
  // screen reader (StepTimeline's dot sits beside visible state text; this one
  // doesn't). `still` keeps a settled snapshot from pinging.
  return <StatusDot status={parked && activeNow ? "waiting" : group.state} still={settled} />;
}

/** Exported for the settled-liveness render pin in route.test.tsx (the settled root
 *  renders collapsed, so the pin drives this row directly). */
export const StepGroupRow = memo(function StepGroupRow({
  group,
  settled,
  parked = false,
  open,
  onOpenChange,
}: {
  group: ExecGroup;
  /** The ITEM is settled: nothing in here may claim liveness. A cancel mid-step leaves
   *  `running` states and pending rows behind (no closing frame ever arrives), and a
   *  settled snapshot must not spin, ping, or tick a clock over them. */
  settled: boolean;
  /** The task waits on a person: a step that still reads `running` (no step frame says
   *  otherwise) is parked on the gate, and its dot says so instead of claiming work. */
  parked?: boolean;
  open: boolean;
  onOpenChange: (id: string, open: boolean) => void;
}) {
  const activeNow = !settled && ACTIVE_STEP_STATES.has(group.state) && group.kind === "step";
  const liveElapsed = useLiveElapsed(group.startedAt, activeNow);
  // Every model the step used, not only the first (a step's helper calls run on another one).
  const models = modelsOf(group);
  const stats = [
    group.attempt > 1 ? `×${group.attempt}` : null,
    groupLedger(group),
    liveElapsed ?? groupDuration(group),
  ].filter(Boolean);

  return (
    <Collapsible.Root open={open} onOpenChange={(o) => onOpenChange(group.id, o)}>
      {/* TWO TIERS (the owner's screenshot): the single line made the model
          ref fight the title for width - titles truncated mid-word while the meta
          repeated at full length and nothing aligned. Line 1 is the STORY (state · title
          · agent · chevron); line 2 is the RECEIPT (model in mono, calls · tokens · cost
          · time in tabular figures), every datum kept, none of it shouting. */}
      <Collapsible.Trigger className="hover:bg-muted/40 group flex w-full items-start gap-2.5 rounded-md px-1.5 py-1.5 text-left transition-colors">
        <span className="flex h-5 shrink-0 items-center">
          <GroupMark group={group} settled={settled} parked={parked} activeNow={activeNow} />
        </span>
        <span className="min-w-0 flex-1">
          <span className="flex items-center gap-2">
            <span
              className={cn(
                "min-w-0 flex-1 truncate text-body-sm-regular",
                activeNow ? "font-medium" : "text-muted-foreground",
              )}
            >
              {headline(group)}
            </span>
            {outcomeLabel(group) && (
              <Badge variant="grey" size="xs" label={outcomeLabel(group) ?? ""} />
            )}
            {/* WHO is doing this - the agent's name as a first-class chip, never buried
                in a truncating meta line ("which agent is working?"). Badge =
                kind word (the Pill-vs-Badge law), wearing its ramp hue: identity color by
                the select-option index law, so the same agent reads the same everywhere. */}
            {group.agent && (
              // The SYSTEM TAG treatment wore mono caps + tracking;
              // Propel's Badge takes no className, so the tag keeps its identity
              // hue as a Propel variant and the DOM keeps the real lowercase registry id.
              <Badge
                variant={AGENT_BADGE[agentHueIndex(group.agent)]}
                size="xs"
                label={`${group.agent}${group.botVersion ? ` v${group.botVersion}` : ""}`}
              />
            )}
            <DisclosureChevron className="size-3 shrink-0" />
          </span>
          {(models.length > 0 || stats.length > 0) && (
            <span className="text-muted-foreground/60 mt-0.5 flex min-w-0 items-baseline text-caption-md-regular">
              {models.length > 0 && (
                <span className="min-w-0 truncate font-mono" title={models.join(", ")}>
                  {models.join(", ")}
                </span>
              )}
              {models.length > 0 && stats.length > 0 && <span className="shrink-0 px-1">·</span>}
              {stats.length > 0 && (
                <span className="shrink-0 tabular-nums">{stats.join(" · ")}</span>
              )}
            </span>
          )}
        </span>
      </Collapsible.Trigger>
      {/* The indent rail - the same hand-border StepTimeline draws (border-l here, a
          w-px strut there: one visual law, two techniques). */}
      <Collapsible.Panel
        className="border-border mt-1 mb-2 ml-[13px] border-l pl-4"
        // Focus inside the panel latches its override open: the auto-open moving to the
        // next group must not unmount a panel under the user's keyboard - that drops
        // focus to <body>.
        onFocusCapture={() => onOpenChange(group.id, true)}
      >
        <GroupBody group={group} settled={settled} parked={parked} />
      </Collapsible.Panel>
    </Collapsible.Root>
  );
});

function GroupBody({
  group,
  settled,
  parked,
}: {
  group: ExecGroup;
  settled: boolean;
  parked: boolean;
}) {
  const [showAll, setShowAll] = useState(false);
  const rows = group.rows;
  // Live groups anchor the visible window to the TAIL (newest activity in view);
  // settled groups read from the head.
  const activeGroup = !settled && ACTIVE_STEP_STATES.has(group.state);
  const clamped =
    !showAll && rows.length > MAX_VISIBLE_ROWS
      ? activeGroup
        ? rows.slice(rows.length - MAX_VISIBLE_ROWS)
        : rows.slice(0, MAX_VISIBLE_ROWS)
      : rows;
  const hidden = rows.length - clamped.length;

  return (
    <div className="space-y-1.5 py-1.5">
      {group.checklist && group.checklist.length > 0 && (
        <ol className="space-y-1.5 pb-1.5">
          {group.checklist.map((c) => (
            <li key={c.id} className="flex items-center gap-2 text-body-sm-regular">
              {c.state === "done" ? (
                <Check className="text-success size-3.5 shrink-0" />
              ) : c.state === "skipped" ? (
                // A skip is not a success - the mark says "passed over", muted.
                <Minus className="text-muted-foreground/60 size-3.5 shrink-0" />
              ) : !settled && ACTIVE_STEP_STATES.has(c.state) ? (
                <Liveness className="size-3.5 shrink-0" />
              ) : (
                <Circle className="text-muted-foreground/50 size-3.5 shrink-0" />
              )}
              <span
                className={cn(
                  "text-muted-foreground min-w-0 truncate",
                  (c.state === "done" || c.state === "skipped") &&
                    "decoration-muted-foreground/40 line-through",
                )}
              >
                {c.title}
              </span>
            </li>
          ))}
        </ol>
      )}
      {group.acceptance && group.acceptance.length > 0 && (
        <ul className="text-muted-foreground/80 space-y-1 pb-1.5 text-caption-md-regular">
          {group.acceptance.map((a) => (
            <li key={a} className="truncate" title={a}>
              {a}
            </li>
          ))}
        </ul>
      )}
      {clamped.map((row) => (
        <ActivityRow key={row.id} row={row} settled={settled} />
      ))}
      {hidden > 0 && (
        <button
          type="button"
          onClick={() => setShowAll(true)}
          className={cn(
            "text-muted-foreground/70 hover:text-foreground flex items-center pt-1 text-caption-md-regular transition-colors",
            TEXT_COL_PAD,
          )}
        >
          +{hidden} more
        </button>
      )}
      {/* The no-dead-air shimmer: an active group with nothing to show yet still breathes -
          unless the task waits on a person, when the tail already says so. */}
      {activeGroup && !parked && rows.length === 0 && !group.checklist?.length && (
        <p className="text-muted-foreground shimmer text-body-sm-regular">working…</p>
      )}
    </div>
  );
}

const ActivityRow = memo(function ActivityRow({
  row,
  settled,
}: {
  row: ExecRow;
  /** Settled items keep their interrupted rows' words ("Reading …", "Thinking…") - that
   *  is what was happening when the turn stopped - but none of the motion. */
  settled: boolean;
}) {
  if (row.t === "note") {
    const loud = row.tone === "warning" || row.tone === "danger";
    // Tone rides the icon, never the words: a loud note wears a small amber mark and
    // speaks in ink. Quiet notes indent to the text column so the panel reads as one
    // left edge.
    if (loud) {
      return (
        <div className="flex items-start gap-2 py-0.5">
          <AlertTriangle className="text-warning mt-0.5 size-3.5 shrink-0" />
          <p className="text-muted-foreground min-w-0 text-caption-md-regular leading-relaxed">
            {row.text}
          </p>
        </div>
      );
    }
    return (
      <p
        className={cn(
          "text-muted-foreground py-0.5 text-caption-md-regular leading-relaxed",
          TEXT_COL_PAD,
        )}
      >
        {row.text}
      </p>
    );
  }
  if (row.t === "round") {
    const Icon = ROUND_ICON;
    const live = !settled && row.pending;
    return (
      <div className="text-muted-foreground flex items-center gap-2 py-0.5 text-body-sm-regular">
        {live ? <Liveness className="size-3.5 shrink-0" /> : <Icon className="size-3.5 shrink-0" />}
        <span className={cn("min-w-0 truncate", live && "shimmer")}>{roundLine(row)}</span>
        {row.thinkingText && (
          <span className="text-muted-foreground/60 min-w-0 truncate text-caption-md-regular italic">
            {row.thinkingText.slice(-120)}
          </span>
        )}
      </div>
    );
  }
  const Icon = toolIcon(row);
  const { verb, detail, mono } = toolLine(row);
  // A held call never ran because a steer landed - quiet, not a refusal.
  const denied = row.ok === false && !isHeld(row);
  const actor = actorLabel(row.tool);
  return (
    <div className="flex items-center gap-2 py-0.5 text-body-sm-regular">
      {!settled && row.pending ? (
        <Liveness className="size-3.5 shrink-0" />
      ) : (
        <Icon
          className={cn("size-3.5 shrink-0", denied ? "text-warning" : "text-muted-foreground")}
        />
      )}
      {/* The machinery actor, named - "Clarifier · Reviewing your request" */}
      {actor && (
        <span className="text-muted-foreground/70 shrink-0 text-caption-md-regular">{actor} ·</span>
      )}
      {verb && <span className="text-muted-foreground shrink-0">{verb}</span>}
      {detail && (
        <span
          className={cn(
            "text-muted-foreground/80 min-w-0 flex-1 truncate",
            mono ? "font-mono text-caption-md-regular" : "text-body-sm-regular",
          )}
          title={detail}
        >
          {detail}
        </span>
      )}
      {row.durationMs != null && row.durationMs > 0 && !row.pending && (
        <span className="text-muted-foreground/60 shrink-0 text-caption-md-regular tabular-nums">
          {formatDurationMs(row.durationMs)}
        </span>
      )}
    </div>
  );
});
