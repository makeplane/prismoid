/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The WHEN dial (ADR-048) - the composer's one new control, and the constitution made
// visible: Send means "now"; setting a When makes the SAME payload a saved routine. The pill
// always shows the When as a sentence - there is no builder, and there never will be (the
// no-builder law: One plans every run).

import { Icon } from "@makeplane/propel/components/icon";
import { Input, InputGroup } from "@makeplane/propel/components/input";
import {
  Popover,
  PopoverContent,
  PopoverDescription,
  PopoverTitle,
  PopoverTrigger,
} from "@makeplane/propel/components/popover";
import { Button as ButtonElement } from "@makeplane/propel/elements/button";
import { AlarmClock } from "lucide-react";
import { useState } from "react";
import { cn } from "~/lib/utils";

export type WhenValue =
  | { mode: "now" }
  | { mode: "schedule"; cron?: string; every?: string }
  | { mode: "event"; kinds: string[] };

export const WHEN_NOW: WhenValue = { mode: "now" };

// Exported: the /routines creation dialog (new-routine.tsx) speaks the SAME When vocabulary -
// one list of presets and event kinds, never two drifting copies.
export const PRESETS: { label: string; cron: string }[] = [
  { label: "Every weekday, 9:00", cron: "0 9 * * 1-5" },
  { label: "Every day, 9:00", cron: "0 9 * * *" },
  { label: "Every hour", cron: "0 * * * *" },
  { label: "Mondays, 9:00", cron: "0 9 * * 1" },
];

export const EVENT_KINDS: { kind: string; label: string }[] = [
  { kind: "task.delivered", label: "a task delivers" },
  { kind: "task.failed", label: "a task fails" },
  { kind: "escalation.raised", label: "a task escalates" },
  { kind: "verification.signed", label: "a receipt is signed" },
  { kind: "artifact.produced", label: "a deliverable is published" },
];

/** The pill's sentence - the whole product copy for a When. */
export function whenLabel(w: WhenValue): string {
  if (w.mode === "now") return "Now";
  if (w.mode === "schedule")
    return w.cron ? `cron ${w.cron}` : w.every ? `every ${w.every}` : "on a schedule";
  return w.kinds.length ? `when ${w.kinds.join(" or ")}` : "on an event";
}

export function WhenDial({
  value,
  onChange,
  defaultOpen = false,
  onOpenChange,
}: {
  value: WhenValue;
  onChange: (w: WhenValue) => void;
  /** Mount with the panel open: the Options menu's "Custom schedule or event" lands here. */
  defaultOpen?: boolean;
  onOpenChange?: (open: boolean) => void;
}) {
  const [open, setOpenState] = useState(defaultOpen);
  const setOpen = (next: boolean) => {
    setOpenState(next);
    onOpenChange?.(next);
  };
  const [cronDraft, setCronDraft] = useState("");
  const [everyDraft, setEveryDraft] = useState("");
  const standing = value.mode !== "now";

  const pick = (w: WhenValue) => {
    onChange(w);
    // an explicit choice supersedes anything typed - a stale draft must not override it on close
    setCronDraft("");
    setEveryDraft("");
    setOpen(false);
  };
  const toggleKind = (kind: string) => {
    const kinds = value.mode === "event" ? value.kinds : [];
    const next = kinds.includes(kind) ? kinds.filter((k) => k !== kind) : [...kinds, kind];
    onChange({ mode: "event", kinds: next });
  };
  // Closing the popover COMMITS a typed schedule draft (when nothing else was chosen): Enter
  // was the only commit path, so typing a cron and clicking back into the goal field silently
  // kept mode="now" - and Send launched an immediate task instead of saving the routine the
  // user just typed.
  const closeCommitsDraft = (next: boolean) => {
    if (!next && value.mode === "now") {
      if (cronDraft.trim()) onChange({ mode: "schedule", cron: cronDraft.trim() });
      else if (everyDraft.trim()) onChange({ mode: "schedule", every: everyDraft.trim() });
    }
    setOpen(next);
  };

  return (
    <Popover open={open} onOpenChange={closeCommitsDraft}>
      <PopoverTrigger
        render={<ButtonElement variant="ghost" size="md" stretch="auto" />}
        aria-label="When this runs"
        title="When: now, on a schedule, or when an event lands (a schedule or event saves this as a routine)"
      >
        <Icon icon={AlarmClock} />
        {standing ? (
          <span className="max-w-40 truncate">{whenLabel(value)}</span>
        ) : (
          <span className="sr-only">When: now</span>
        )}
      </PopoverTrigger>
      <PopoverContent align="start">
        {/* The width box: Propel's rich panel is w-max, so the copy would run one long line. */}
        <div className="w-72 space-y-4">
          <div className="space-y-1">
            <PopoverTitle>When should this run?</PopoverTitle>
            <PopoverDescription>
              Now sends it once. A schedule or an event saves it as a routine - same task, standing.
            </PopoverDescription>
          </div>

          <button
            type="button"
            onClick={() => pick(WHEN_NOW)}
            className={cn(
              "hover:bg-accent/50 w-full rounded-md border px-3 py-2 text-left text-body-sm-regular",
              value.mode === "now" && "border-primary",
            )}
          >
            Now - send once
          </button>

          <div className="space-y-2">
            {/* Section eyebrows: they head a group of buttons, they label no control. */}
            <p className="text-muted-foreground text-caption-md-regular font-medium uppercase">
              On a schedule
            </p>
            <div className="grid grid-cols-2 gap-1.5">
              {PRESETS.map((p) => (
                <button
                  key={p.cron}
                  type="button"
                  onClick={() => pick({ mode: "schedule", cron: p.cron })}
                  className={cn(
                    "hover:bg-accent/50 rounded-md border px-2.5 py-1.5 text-left text-caption-md-regular",
                    value.mode === "schedule" && value.cron === p.cron && "border-primary",
                  )}
                >
                  {p.label}
                </button>
              ))}
            </div>
            <div className="flex gap-1.5">
              <div className="min-w-0 flex-1">
                <InputGroup size="md">
                  <Input
                    size="md"
                    value={cronDraft}
                    onChange={(e) => setCronDraft(e.target.value)}
                    placeholder='cron, e.g. "30 8 * * 1-5"'
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && cronDraft.trim())
                        pick({ mode: "schedule", cron: cronDraft.trim() });
                    }}
                  />
                </InputGroup>
              </div>
              <div className="w-24">
                <InputGroup size="md">
                  <Input
                    size="md"
                    value={everyDraft}
                    onChange={(e) => setEveryDraft(e.target.value)}
                    placeholder='every, e.g. "4h"'
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && everyDraft.trim())
                        pick({ mode: "schedule", every: everyDraft.trim() });
                    }}
                  />
                </InputGroup>
              </div>
            </div>
          </div>

          <div className="space-y-2">
            <p className="text-muted-foreground text-caption-md-regular font-medium uppercase">
              When an event lands
            </p>
            <div className="space-y-1">
              {EVENT_KINDS.map((e) => {
                const on = value.mode === "event" && value.kinds.includes(e.kind);
                return (
                  <button
                    key={e.kind}
                    type="button"
                    onClick={() => toggleKind(e.kind)}
                    className={cn(
                      "hover:bg-accent/50 flex w-full items-center justify-between rounded-md border px-2.5 py-1.5 text-left text-caption-md-regular",
                      on && "border-primary",
                    )}
                  >
                    {e.label}
                    {on && <span aria-hidden>✓</span>}
                  </button>
                );
              })}
            </div>
            <p className="text-muted-foreground text-caption-md-regular">
              Event routines start held: each run waits for your release until you flip it.
            </p>
          </div>
        </div>
      </PopoverContent>
    </Popover>
  );
}
