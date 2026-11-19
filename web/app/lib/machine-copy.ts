/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import type {
  MachineControlClaimed,
  MachineControlEnded,
  MachineCreated,
  MachineRebuilt,
  MachineReset,
  MachineSlept,
  MachineWoke,
} from "./api/events";
import { personName } from "./format";

type MachineEvent =
  | MachineCreated
  | MachineSlept
  | MachineWoke
  | MachineRebuilt
  | MachineReset
  | MachineControlClaimed
  | MachineControlEnded;

const WORDS: Record<string, string> = {
  waking: "Running",
  running: "Running",
  parked: "Waiting",
  idle: "Idle",
  sleeping: "Asleep",
  asleep: "Asleep",
  gone: "Gone",
};

export function machineWord(state: string): string {
  return WORDS[state] ?? state;
}

/** The computer as a person reads it: its own name, else "your computer" (device-machine spec
 *  O13). Keep in step with the backend's `schemas.device.device_label`. */
export function deviceLabel(name?: string | null): string {
  return name?.trim() || "your computer";
}

/** Where a device Machine's browser is: a window on the user's own computer. */
export function browserOn(name?: string | null): string {
  return `Browser on ${deviceLabel(name)}`;
}

/** The screen pane opens only on a task that has a Machine, and never on a device Machine: its
 *  browser is a window on the user's own computer, with no screen to relay. */
export function screenPaneOpens(
  wanted: boolean,
  machine: { driver?: string | null } | null | undefined,
): boolean {
  return wanted && machine != null && machine.driver !== "device";
}

/** A gate as the docks draw it: on a device Machine it names its driver, so a CONTROL gate offers
 *  the creator's Done instead of the screen (the task's events never carry the driver). */
export function controlGateFor<G extends { machine_driver?: string | null }>(
  gate: G,
  machine: { driver?: string | null } | null | undefined,
): G {
  return machine?.driver === "device" ? { ...gate, machine_driver: "device" } : gate;
}

export function minutes(seconds: number): number {
  return Math.round(seconds / 60);
}

/** How long someone held the browser: "45s", "1m 40s", "2h 3m". Keep in step with the backend's
 *  `schemas.screen.held_text`, which words the agent's resume text the same way. */
export function heldText(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s}s`;
  if (s < 3600) {
    const m = Math.floor(s / 60);
    const r = s % 60;
    return r ? `${m}m ${r}s` : `${m}m`;
  }
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return m ? `${h}h ${m}m` : `${h}h`;
}

function gb(bytes: number): string {
  return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
}

/** The timeline row for a lease's ending. `who` held it. */
function controlEndedText(ev: MachineControlEnded, who: string): string {
  const held = heldText(ev.held_s ?? 0);
  switch (ev.reason) {
    case "released":
      return `${who} handed back control after ${held}`;
    case "expired":
      return `${who}'s control expired after a minute with no input`;
    case "ended":
      return `${ev.ended_by ? personName(ev.ended_by.name) : "Someone"} ended ${who}'s control`;
    case "grace":
      return `${who} left the screen, so their control ended after ${held}`;
    case "cap":
      return `${who}'s private control reached its time limit after ${held}`;
    case "demoted":
      return `${who} can only view this Project now, so their control ended`;
    case "disabled":
      return `${who}'s control ended: computer use was turned off`;
    case "lost":
      return `${who}'s control ended: the Machine lost its browser`;
    default:
      return `${who}'s control ended after ${held}`;
  }
}

/** What the Machine's last created/woke event said, so a wake can be worded for what changed. */
export type MachinePrev = { profile?: string | null; turn?: number | null };

export function machineMarkerText(ev: MachineEvent, prev: MachinePrev = {}): string {
  const who = ev.actor ? personName(ev.actor.name) : null;
  const someone = who ?? "Someone";
  switch (ev.type) {
    case "machine.created":
      // The event says the Machine's row was made, not that a frame booted: "created" claims no more.
      return ev.seeded_as_of ? "Machine created from the parent's files" : "Machine created";
    case "machine.slept": {
      const idleMin = minutes(ev.after_s ?? 0);
      if (ev.dropped_bytes)
        return `Machine slept: ${gb(ev.dropped_bytes)} of files were over the 2 GB limit and were not kept`;
      if (ev.reason === "manual") return `${someone} put the Machine to sleep`;
      if (ev.reason === "cap") return "Machine slept to make room for another task";
      if (ev.reason === "upgrade") return "Machine moved to the browser image";
      if (ev.reason === "park_hold") return `Machine slept while waiting (${idleMin} min)`;
      // Only a reason the event names is worded: an idle sleep says how long it sat unused, any
      // other says just that it slept - never a guess about why.
      if (ev.reason === "idle") return `Machine slept after ${idleMin} min unused`;
      return "Machine slept";
    }
    case "machine.woke": {
      if (ev.from_park) return "Machine woke to continue the step";
      if (prev.profile && ev.profile && ev.profile !== prev.profile)
        return `Machine woke on the ${ev.profile} image`;
      // A follow-up is a LATER turn than the Machine's last one; the same turn's wake (a retry,
      // a resumed step) is not one, whoever's turn it is.
      const followUp = who && prev.turn != null && ev.turn > prev.turn;
      const base = followUp ? `Machine woke for ${who}'s follow-up` : "Machine woke";
      return ev.browser_restored === false ? `${base}; browser logins could not be restored` : base;
    }
    case "machine.rebuilt":
      // `lost` covers a frame that died (the work since its last save is gone) and a checkpoint
      // that vanished: some files are gone either way, so the line claims no more than that.
      if (ev.reason === "expired")
        return `Machine rebuilt: its saved files expired after ${ev.retention_days ?? 14} days`;
      if (ev.reason === "oversize") return "Machine rebuilt: its files were too large to save";
      return "Machine lost some of its files";
    case "machine.reset":
      return `${someone} reset the Machine`;
    case "machine.control_claimed":
      return `${someone} took control of the browser${ev.private ? " (private)" : ""}`;
    case "machine.control_ended":
      return controlEndedText(ev, someone);
  }
}
