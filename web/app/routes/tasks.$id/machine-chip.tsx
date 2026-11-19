/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { memo, useId, useState } from "react";

import { Button } from "@makeplane/propel/components/button";
import {
  AlertDialog,
  AlertDialogActions,
  AlertDialogBody,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogHeader,
  AlertDialogIntro,
  AlertDialogTitle,
} from "@makeplane/propel/components/alert-dialog";
import { Money } from "~/components/kit";
import { machineWord, minutes } from "~/lib/machine-copy";
import { cn } from "~/lib/utils";

export type MachineView = {
  state: string;
  profile: string;
  state_since: string;
  last_active_at: string;
  checkpoint_at?: string | null;
  checkpoint_bytes?: number | null;
  browser_saved?: boolean;
  warm_seconds?: number;
  /** The Machine's warm time in USD, the server's decimal string. */
  cost_usd?: string | number;
  /** The live control lease: who drives the browser, if anyone. */
  control?: {
    holder: { id: string; name: string };
    private: boolean;
    since: string;
    until: string;
    cap_at?: string | null;
  } | null;
  /** The computer a device Machine runs on; null on a cloud driver. */
  device_name?: string | null;
  /** "device" for a Machine on the user's own computer, which has no screen. */
  driver?: "device" | null;
};

const SLEEPABLE = new Set(["idle", "parked"]);
const RESETTABLE = new Set(["idle", "parked", "asleep"]);
const DOT: Record<string, string> = {
  Running: "bg-primary",
  Waiting: "bg-warning",
  Idle: "bg-muted-foreground",
  Asleep: "bg-muted-foreground/50",
  Gone: "bg-border",
};

/** Is the chip behind the run? A parked Machine re-binds without a machine.* frame when its
 *  step resumes (a pause lifted, a question answered): until the chip re-reads, it says parked
 *  ("Waiting") while the task runs with no gate open. The page re-reads it on the next record
 *  frame while this holds - so the chip turns once the step has the Machine again. */
export function chipBehindTheRun(
  machine: Pick<MachineView, "state"> | null | undefined,
  taskStatus: string,
  openGates: number,
): boolean {
  return machine?.state === "parked" && taskStatus === "running" && openGates === 0;
}

function clock(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export const MachineChip = memo(function MachineChip({
  machine,
  canEdit,
  onSleep,
  onReset,
}: {
  machine: MachineView;
  canEdit: boolean;
  onSleep: () => void;
  onReset: () => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const hintId = useId();
  const word = machineWord(machine.state);
  const busy = machine.state === "running" || machine.state === "waking";
  // A Machine someone drives is never slept or reset under them: the server answers
  // 409 "{name} is driving the browser.", so the buttons say it first. The chip never reads a clock: the lease it is given comes from
  // the server only (screen.state, reseeded from the task read). A holder with no name is someone.
  const driving = machine.control != null;
  const hint = driving
    ? `${machine.control?.holder.name || "Someone"} is driving the browser.`
    : busy
      ? "It's working"
      : null;
  return (
    <div className="flex flex-col gap-1 text-body-sm-regular">
      <div className="flex items-center gap-2">
        <span aria-hidden className={cn("size-2 rounded-full", DOT[word] ?? "bg-border")} />
        <span className="font-medium">
          {word} · {machine.profile}
        </span>
      </div>
      <div className="text-muted-foreground text-caption-md-regular">
        since {clock(machine.state_since)}
      </div>
      <div className="text-muted-foreground text-caption-md-regular tabular-nums">
        Machine time {minutes(machine.warm_seconds ?? 0)} min ·{" "}
        <Money usd={machine.cost_usd ?? 0} />
      </div>
      {canEdit ? (
        <div className="mt-1 flex gap-2">
          <Button
            size="sm"
            stretch="auto"
            variant="secondary"
            label="Sleep now"
            disabled={!SLEEPABLE.has(machine.state) || driving}
            aria-describedby={hint ? hintId : undefined}
            onClick={onSleep}
          />
          <Button
            size="sm"
            stretch="auto"
            variant="ghost"
            label="Reset…"
            disabled={!RESETTABLE.has(machine.state) || driving}
            aria-describedby={hint ? hintId : undefined}
            onClick={() => setConfirming(true)}
          />
        </div>
      ) : null}
      {canEdit && hint ? (
        <span id={hintId} className="text-muted-foreground text-caption-md-regular">
          {hint}
        </span>
      ) : null}
      <AlertDialog open={confirming} onOpenChange={setConfirming}>
        <AlertDialogContent>
          <AlertDialogBody>
            <AlertDialogHeader>
              <AlertDialogIntro>
                <AlertDialogTitle>Reset the Machine?</AlertDialogTitle>
                <AlertDialogDescription>
                  Its files and browser logins are deleted. The next step starts clean, with the
                  task's deliverables.
                </AlertDialogDescription>
              </AlertDialogIntro>
            </AlertDialogHeader>
          </AlertDialogBody>
          <AlertDialogActions>
            <Button
              variant="ghost"
              size="md"
              stretch="auto"
              label="Cancel"
              onClick={() => setConfirming(false)}
            />
            <Button
              variant="danger"
              size="md"
              stretch="auto"
              label="Reset"
              onClick={() => {
                setConfirming(false);
                onReset();
              }}
            />
          </AlertDialogActions>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
});
