/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import {
  AlertTriangle,
  Ban,
  Check,
  CircleDashed,
  CircleSlash,
  CirclePause,
  Clock,
  LoaderCircle,
  MessageCircleQuestion,
  X,
  type LucideIcon,
} from "lucide-react";
import { cn } from "~/lib/utils";
import type { TaskStatus, StepState } from "~/lib/api/events";
import { TONE_TINT, TONE_TEXT, type Tone } from "./tone";
import { Liveness } from "./liveness";

const pill = (tone: Tone) =>
  cn(
    "inline-flex items-center gap-1.5 rounded-pill border px-2 py-0.5 text-caption-md-regular font-medium capitalize",
    TONE_TINT[tone],
  );

const TASK_TONE: Record<TaskStatus, Tone> = {
  created: "muted",
  clarifying: "warning",
  planning: "info",
  running: "info",
  waiting: "warning",
  paused: "warning",
  delivered: "success",
  failed: "danger",
  cancelled: "muted",
};

const STEP_TONE: Record<StepState, Tone> = {
  pending: "muted",
  ready: "muted",
  running: "info",
  verifying: "info",
  done: "success",
  retrying: "warning",
  escalated: "danger",
  skipped: "muted",
};

/** The mark a settled task wears: an honest could-not (`ending`) reads apart from a fault or a done
 *  answer - its own neutral mark, never the red X or the green check; a partly-done answer is not
 *  a green check either. */
export function taskMark(status: string, ending?: string | null): string {
  if (status === "delivered" && ending === PARTIAL) return PARTIAL;
  return ending === "could_not" && (status === "failed" || status === "delivered")
    ? COULD_NOT
    : status;
}

const COULD_NOT = "could_not";
const PARTIAL = "partial";

export function statusTone(status: string): Tone {
  return TASK_TONE[status as TaskStatus] ?? STEP_TONE[status as StepState] ?? "muted";
}

/**
 * Human label for a machine status. The wire keeps `created`; every surface says
 * "queued" - nothing has happened yet, and "created" reads like a database row, not work.
 */
export function statusLabel(status: string): string {
  if (status === "created") return "queued";
  if (status === COULD_NOT) return "couldn't do it";
  if (status === PARTIAL) return "partly done";
  return status.replace(/_/g, " ");
}

// The dot's SHAPE encodes lifecycle phase (tone keeps carrying sentiment): a hollow ring is
// work that hasn't run (queued, pending, paused), a rippling halo is work in motion RIGHT NOW
// (the machine's states - and a gate waiting on you, which deserves the same pull), a solid
// fill is settled. Colors only ever come from tokens: text-* on the wrapper, currentColor
// underneath, so every layer stays theme-correct in both schemes.
const LIVE = new Set(["planning", "running", "verifying", "retrying", "clarifying", "waiting"]);
const HOLLOW = new Set(["created", "pending", "ready", "paused"]);

/** A colored status dot - for dense rows where a full pill is too much. Hollow = not yet
 *  run · rippling halo = live · solid = settled; the ripple hides under reduced motion. */
export function StatusDot({
  status,
  className,
  decorative = false,
  still = false,
}: {
  status: string;
  className?: string;
  /** Set when adjacent visible text already states the same status. */
  decorative?: boolean;
  /** Suppress the live ripple: the ripple is a promise of motion RIGHT NOW, and a settled
   *  snapshot showing an interrupted `running` state must not make it. */
  still?: boolean;
}) {
  const tone = statusTone(status);
  const label = `Status: ${statusLabel(status)}`;
  return (
    <span
      className={cn("relative inline-flex size-2 shrink-0", TONE_TEXT[tone], className)}
      title={statusLabel(status)}
      role={decorative ? undefined : "img"}
      aria-label={decorative ? undefined : label}
      aria-hidden={decorative || undefined}
    >
      {!still && LIVE.has(status) && (
        <span
          aria-hidden
          className="animate-status-ping motion-reduce:hidden absolute inset-0 rounded-full bg-current"
        />
      )}
      <span
        aria-hidden
        className={cn(
          "relative size-2 rounded-full",
          HOLLOW.has(status) ? "ring-current ring-1 ring-inset" : "bg-current",
        )}
      />
    </span>
  );
}

// The mark vocabulary: state as a GLYPH, not a colored dot - a wall of green dots says
// nothing ("green dot is a hard thing to do"). Working states SPIN, always,
// the universal "alive right now"; needs-input is a speech bubble; queued is a clock;
// settled states are their verdict. Tones still ride TONE_TEXT.
const MARKS: Record<string, { icon: LucideIcon; tone: Tone; spin?: boolean }> = {
  // task statuses
  created: { icon: Clock, tone: "muted" },
  clarifying: { icon: MessageCircleQuestion, tone: "warning" },
  planning: { icon: LoaderCircle, tone: "info", spin: true },
  running: { icon: LoaderCircle, tone: "info", spin: true },
  waiting: { icon: MessageCircleQuestion, tone: "warning" },
  paused: { icon: CirclePause, tone: "warning" },
  delivered: { icon: Check, tone: "success" },
  failed: { icon: X, tone: "danger" },
  cancelled: { icon: Ban, tone: "muted" },
  could_not: { icon: CircleSlash, tone: "muted" },
  partial: { icon: CircleDashed, tone: "warning" },
  // step states, mapped onto the same vocabulary
  pending: { icon: Clock, tone: "muted" },
  ready: { icon: Clock, tone: "muted" },
  verifying: { icon: LoaderCircle, tone: "info", spin: true },
  retrying: { icon: LoaderCircle, tone: "warning", spin: true },
  done: { icon: Check, tone: "success" },
  escalated: { icon: AlertTriangle, tone: "danger" },
  skipped: { icon: Ban, tone: "muted" },
};

/** The words a labelled mark carries beside its glyph: only a state where the NEXT move is the
 *  person's, so a glyph alone ("?") never has to be decoded. */
const MARK_WORD: Record<string, string> = { waiting: "Needs you" };

/** True when this status's mark SPINS (alive right now) - the topbar hides its mark for
 *  these states (the tail owns liveness); every settled/parked mark stays. */
export function statusSpins(status: string): boolean {
  return MARKS[status]?.spin === true;
}

/** The status GLYPH - task rows, board columns, the topbar. Same a11y contract as
 *  StatusDot (role img / "Status: …" / `decorative` opt-out); the spin honours reduced
 *  motion. StatusDot remains for micro contexts (the step timeline's 8px marks). */
export function StatusMark({
  status,
  className,
  decorative = false,
  still = false,
  labelled = false,
}: {
  status: string;
  className?: string;
  decorative?: boolean;
  /** Say the state in words beside the glyph when it has some ("Needs you"). */
  labelled?: boolean;
  /** Motion is liveness - a mark that stands for a CATEGORY (an empty board column)
   *  rather than a live thing falls still; the glyph and tone stay. */
  still?: boolean;
}) {
  const mark = MARKS[status] ?? { icon: Clock, tone: "muted" as Tone };
  const Icon = mark.icon;
  const label = `Status: ${statusLabel(status)}`;
  const word = labelled ? MARK_WORD[status] : undefined;
  if (word)
    return (
      <span
        className={cn(
          "inline-flex shrink-0 items-center gap-1.5 text-caption-md-regular font-medium",
          TONE_TEXT[mark.tone],
          className,
        )}
      >
        <Icon className="size-4" aria-hidden />
        {word}
      </span>
    );
  return (
    <span
      className={cn(
        "flex size-4 shrink-0 items-center justify-center",
        TONE_TEXT[mark.tone],
        className,
      )}
      title={statusLabel(status)}
      role={decorative ? undefined : "img"}
      aria-label={decorative ? undefined : label}
      aria-hidden={decorative || undefined}
    >
      {mark.spin && !still ? (
        // Alive right now: the fade trio (kit/liveness), never the rotating arc.
        // `still` snapshots keep the frozen glyph - a settled record must not imply motion.
        <Liveness className="size-3.5" />
      ) : (
        <Icon className="size-4" />
      )}
    </span>
  );
}

/** The full status pill. */
export function StatusBadge({ status, className }: { status: string; className?: string }) {
  return <span className={cn(pill(statusTone(status)), className)}>{statusLabel(status)}</span>;
}
