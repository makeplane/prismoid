/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/**
 * TaskRail - the task's working state, one glanceable column.
 *
 * ## The grammar (four sections, always this order)
 *   Progress → the live plan (spinner · n/m · quiet bar), earlier plans behind a disclosure
 *   Outputs  → every artifact the thread has published; click = open in the reader pane
 *   Sources  → the task's attached inputs (what the work was grounded on); click = download
 *   Usage    → live spend + calls off the ledger, then the context lanes verbatim
 *
 * ## The contract (DX rules - read before touching)
 *   1. PRESENTATIONAL ONLY. TaskRail owns no chrome, no width, no scroll, no fetch - it is
 *      pure content, mounted by the route into whichever container the moment calls for:
 *        · wide screens  → the FLOATING CARD beside the transcript
 *        · narrow        → the overlay Sheet
 *        · reader open   → the centered MODAL behind the topbar's Progress button
 *      One component, three containers (all mounted in route.tsx) - never fork the
 *      content per container.
 *   2. Every section is a <RailSection>: same header, same chevron, same collapse behavior.
 *      A new section is a new <RailSection>, nothing bespoke.
 *   3. Empty sections HIDE (Sources with no inputs, Usage before the first frame) - except
 *      Progress and Outputs, whose empty states are meaningful ("no plan yet", "nothing
 *      published yet") and render as EmptyState.
 *   4. Memoized: a streaming flush re-renders the page many times a second; the rail's
 *      inputs only change at event boundaries. Keep props primitive-stable (the route
 *      passes memoized arrays/callbacks).
 *
 * ## Who closes whom (the page's one-surface-at-a-time law - enforced in route.tsx)
 *   Opening the reader pane hides the rail; closing it restores the rail to its saved
 *   preference. While the reader is open the SAME rail content is one click away behind
 *   the topbar's Progress button, as a modal. The rail never renders beside the open pane.
 */

import { memo, useEffect, useMemo, useState, type ReactNode } from "react";
import {
  Liveness,
  artifactIcon,
  artifactKindLabel,
  DisclosureChevron,
  EmptyState,
  IconTile,
  Money,
  QuietDisclosure,
  StepTimeline,
  VerdictBadge,
} from "~/components/kit";
// Base UI's Collapsible, not Propel's: see kit/disclosure.tsx.
import { Collapsible } from "@base-ui/react/collapsible";
import { LinearProgress } from "@makeplane/propel/components/linear-progress";
import { cn } from "~/lib/utils";
import type { ArtifactView, StepView, TaskView } from "~/lib/task-view";
import { LANE_LABEL, laneBudget } from "./context-meter";
import { MachineChip, type MachineView } from "./machine-chip";
import { ACTIVE_STEP_STATES } from "./progress-copy";
import { DeviceBrowser } from "./device-browser";
import { ScreenThumb } from "./screen-thumb";

/** A superseded or ancestral plan, shown collapsed under the live one. */
export type EarlierPlan = { id: string; label: string; steps: StepView[] };

/** One attached input, as the loader serves it - the Sources section's row. */
export type SourceInput = {
  filename: string;
  bytes: number;
  media_type: string;
  url: string;
};

/** Tracks Tailwind's xl breakpoint (80rem) so the rail can be a floating card on wide screens
 *  and an overlay sheet below - one content component, two containers. */
export function useWideViewport(): boolean {
  const [wide, setWide] = useState(
    () => typeof window !== "undefined" && window.matchMedia("(min-width: 80rem)").matches,
  );
  useEffect(() => {
    const mql = window.matchMedia("(min-width: 80rem)");
    const sync = () => setWide(mql.matches);
    mql.addEventListener("change", sync);
    return () => mql.removeEventListener("change", sync);
  }, []);
  return wide;
}

/** The rail's one section shape: kit-Section's header wearing a collapse chevron. Open by
 *  default; collapse state is per-mount and deliberately unpersisted (a fresh page starts
 *  with everything visible - the rail IS the overview). */
function RailSection({
  title,
  meta,
  defaultOpen = true,
  children,
}: {
  title: string;
  /** Quiet inline annotation beside the title - counts, spinners, spend. */
  meta?: ReactNode;
  defaultOpen?: boolean;
  children: ReactNode;
}) {
  return (
    <Collapsible.Root defaultOpen={defaultOpen}>
      <Collapsible.Trigger className="group flex w-full items-center justify-between text-left">
        <span className="text-muted-foreground flex items-center gap-2 text-caption-md-regular font-medium tracking-wide uppercase">
          {title}
          {/* one row with the title: a count and the liveness mark sit on its baseline */}
          {meta && <span className="flex items-center gap-1.5 normal-case">{meta}</span>}
        </span>
        <DisclosureChevron />
      </Collapsible.Trigger>
      <Collapsible.Panel className="pt-3">{children}</Collapsible.Panel>
    </Collapsible.Root>
  );
}

function noop() {}

/** A stable empty default - a fresh `{}` per render would defeat the rail's memo. */
const NONE_SUPERSEDED: Readonly<Record<string, string>> = {};

/** A Machine has a screen only while it is warm on the browser image; asleep, the
 *  thumbnail disappears (1b's rule: no Wake button, no image). */
const SCREEN_STATES = new Set(["running", "parked", "idle"]);

/** The Machine section's browser line: the screen thumbnail on a cloud Machine. A device Machine
 *  has no screen: its browser is a window on the user's own computer,
 *  so the rail says where - and never dials a screen socket for it. */
function MachineBrowser({
  machine,
  taskId,
  holder,
  settled,
  onOpen,
}: {
  machine: MachineView;
  taskId?: string;
  holder: string | null;
  settled: boolean;
  onOpen: () => void;
}) {
  if (machine.profile !== "browser" || !SCREEN_STATES.has(machine.state)) return null;
  if (machine.driver === "device") return <DeviceBrowser name={machine.device_name} />;
  return taskId ? (
    <ScreenThumb taskId={taskId} holderName={holder} settled={settled} onOpen={onOpen} />
  ) : null;
}

/** History stays out of the way: one quiet disclosure, closed by default. */
function EarlierPlans({ plans }: { plans: EarlierPlan[] }) {
  if (plans.length === 0) return null;
  return (
    <QuietDisclosure
      label="Earlier plans"
      count={plans.length}
      triggerClassName="pt-1"
      panelClassName="space-y-4 pl-0"
    >
      {plans.map((p) => (
        <div key={p.id} className="space-y-2">
          <p className="text-muted-foreground/70 truncate text-caption-md-regular" title={p.label}>
            {p.label}
          </p>
          <StepTimeline steps={p.steps} settled />
        </div>
      ))}
    </QuietDisclosure>
  );
}

/** The Usage section's web-search count, kept apart from model calls; drawn only once one ran. */
function SearchesRow({ searches }: { searches: number }) {
  if (searches <= 0) return null;
  return (
    <div className="flex items-baseline justify-between">
      <dt className="text-muted-foreground">Searches</dt>
      <dd className="tabular-nums">{searches}</dd>
    </div>
  );
}

export const TaskRail = memo(function TaskRail({
  steps,
  earlierPlans = [],
  artifacts,
  inputs = [],
  usage = null,
  context = null,
  machine = null,
  canEdit = false,
  onSleepMachine,
  onResetMachine,
  taskId,
  screenHolder = null,
  onOpenScreen,
  onOpenArtifact,
  superseded = NONE_SUPERSEDED,
  settled = false,
  className,
}: {
  steps: StepView[];
  earlierPlans?: EarlierPlan[];
  artifacts: ArtifactView[];
  inputs?: SourceInput[];
  /** Live ledger figures for the Usage section; null hides it (nothing spent yet). */
  usage?: { usd: string; calls: number; searches: number } | null;
  context?: TaskView["context"];
  /** The task's Machine; null hides the section. */
  machine?: MachineView | null;
  canEdit?: boolean;
  onSleepMachine?: () => void;
  onResetMachine?: () => void;
  /** The task whose screen the thumbnail opens; without it there is no thumbnail. */
  taskId?: string;
  /** Who holds the control lease right now (the route's `currentLease`), for the thumbnail. */
  screenHolder?: string | null;
  /** Opens the screen in the split pane (route.tsx owns the one-surface law). */
  onOpenScreen?: () => void;
  onOpenArtifact: (id: string) => void;
  /** Artifacts a steer's re-run superseded: never on the shelf - the shelf is what
   *  the task made, and a superseded draft is history the feed card keeps. */
  superseded?: Readonly<Record<string, string>>;
  /** The task has ended: nothing is starting, so the screen thumbnail never says it is. */
  settled?: boolean;
  className?: string;
}) {
  const done = steps.filter((s) => s.state === "done").length;
  const shelf = useMemo(
    () => artifacts.filter((a) => !Object.hasOwn(superseded, a.id)),
    [artifacts, superseded],
  );
  const inFlight = steps.some((s) => ACTIVE_STEP_STATES.has(s.state));
  return (
    <div className={cn("flex flex-col gap-6 px-5 py-4", className)}>
      {machine && (
        <RailSection title="Machine">
          <div className="space-y-3">
            <MachineBrowser
              machine={machine}
              taskId={taskId}
              holder={screenHolder}
              settled={settled}
              onOpen={onOpenScreen ?? noop}
            />
            <MachineChip
              machine={machine}
              canEdit={canEdit}
              onSleep={onSleepMachine ?? noop}
              onReset={onResetMachine ?? noop}
            />
          </div>
        </RailSection>
      )}
      {/* ── Progress ──────────────────────────────────────────────────────────────── */}
      <RailSection
        title="Progress"
        meta={
          <>
            {steps.length > 0 && (
              <span className="text-muted-foreground/70 tabular-nums">
                {done} of {steps.length}
              </span>
            )}
            {inFlight && <Liveness className="size-3" />}
          </>
        }
      >
        <div className="space-y-3">
          {/* The bar tracks a plan in motion; once everything is done the count says it -
              a full-width solid stripe on a finished task is noise. */}
          {steps.length > 0 && done < steps.length && (
            <LinearProgress
              value={Math.round((done / steps.length) * 100)}
              size="sm"
              variant="brand"
              showValue={false}
              aria-label="Plan progress"
            />
          )}
          {steps.length === 0 ? (
            <EmptyState
              compact
              title="No plan yet"
              description="Steps appear as planning finishes."
            />
          ) : (
            <StepTimeline steps={steps} />
          )}

          <EarlierPlans plans={earlierPlans} />
        </div>
      </RailSection>

      {/* ── Outputs ───────────────────────────────────────────────────────────────── */}
      <RailSection
        title="Outputs"
        meta={
          shelf.length > 0 ? (
            <span className="text-muted-foreground/70 tabular-nums">· {shelf.length}</span>
          ) : undefined
        }
      >
        {shelf.length === 0 ? (
          <EmptyState
            compact
            title="Nothing published yet"
            description="Deliverables land here as they publish."
          />
        ) : (
          <div className="-mx-2 space-y-0.5">
            {shelf.map((a) => (
              <button
                key={a.id}
                onClick={() => onOpenArtifact(a.id)}
                title={a.title ?? "Artifact"}
                className="hover:bg-accent/40 flex w-full items-center gap-2.5 rounded-md px-2 py-1.5 text-left transition-colors"
              >
                <IconTile icon={artifactIcon(a.type)} className="size-8 rounded-md" />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-body-sm-regular">
                    {a.title ?? "Artifact"}
                  </span>
                  <span className="mt-0.5 flex items-center gap-1.5">
                    <VerdictBadge verdict={a.verdict} showIcon={false} />
                    {a.type && (
                      <span className="text-muted-foreground truncate text-caption-md-regular">
                        {artifactKindLabel(a.type)}
                      </span>
                    )}
                  </span>
                </span>
              </button>
            ))}
          </div>
        )}
      </RailSection>

      {/* ── Sources ───────────────────────────────────────────────────────────────── */}
      {inputs.length > 0 && (
        <RailSection
          title="Sources"
          meta={<span className="text-muted-foreground/70 tabular-nums">· {inputs.length}</span>}
        >
          <div className="-mx-2 space-y-0.5">
            {inputs.map((s, i) => (
              <a
                key={`${s.filename}-${i}`}
                href={s.url}
                download={s.filename}
                title={s.filename}
                className="hover:bg-accent/40 flex w-full items-center gap-2.5 rounded-md px-2 py-1.5 transition-colors"
              >
                <IconTile icon={artifactIcon("document")} className="size-8 rounded-md" />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-body-sm-regular">{s.filename}</span>
                  <span className="text-muted-foreground text-caption-md-regular tabular-nums">
                    {Math.max(1, Math.round(s.bytes / 1024))} KB
                  </span>
                </span>
              </a>
            ))}
          </div>
        </RailSection>
      )}

      {/* ── Usage ─────────────────────────────────────────────────────────────────── */}
      {usage && (
        <RailSection title="Usage" defaultOpen={false}>
          <div className="space-y-3">
            <dl className="space-y-1.5 text-body-sm-regular">
              <div className="flex items-baseline justify-between">
                <dt className="text-muted-foreground">Spend</dt>
                <dd className="tabular-nums">
                  <Money usd={usage.usd} />
                </dd>
              </div>
              <div className="flex items-baseline justify-between">
                <dt className="text-muted-foreground">Model calls</dt>
                <dd className="tabular-nums">{usage.calls}</dd>
              </div>
              <SearchesRow searches={usage.searches} />
            </dl>

            {/* Why did the machine know that - the `context.assembled` record, verbatim:
                every lane it was handed, what each cost in tokens, where the budget bit. */}
            {context && context.lanes.length > 0 && (
              <div className="space-y-2.5 border-t pt-3">
                <p className="text-muted-foreground/70 text-caption-md-regular">
                  Context · {context.totalTokens} tokens
                </p>
                {context.lanes.map((lane) => (
                  <div key={lane.lane}>
                    <p className="text-muted-foreground flex items-baseline gap-1.5 text-caption-md-regular font-medium">
                      {LANE_LABEL[lane.lane] ?? lane.lane}
                      <span className="text-muted-foreground/70 font-regular tabular-nums">
                        {lane.tokens}
                        {laneBudget(lane) !== null && (
                          <> / {laneBudget(lane)?.toLocaleString()}</>
                        )}{" "}
                        tok
                      </span>
                      {lane.truncated && (
                        <span
                          className="text-muted-foreground/70 font-regular"
                          title="This lane hit its token budget. What didn't fit stayed in the ledger, not the prompt."
                        >
                          trimmed
                        </span>
                      )}
                    </p>
                    <ul className="mt-1 space-y-0.5">
                      {lane.items.map((item, i) => (
                        <li
                          key={item.memory_id ?? i}
                          className="text-muted-foreground/90 truncate text-caption-md-regular"
                          title={item.preview}
                        >
                          {item.preview}
                        </li>
                      ))}
                    </ul>
                  </div>
                ))}
              </div>
            )}
          </div>
        </RailSection>
      )}
    </div>
  );
});
