/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The Machine's folds: its lifecycle and control facts as quiet markers that also bump the chip's
// version, and the screen's live state (who drives the browser, where the agent just acted).

import { machineMarkerText } from "~/lib/machine-copy";
import { marker } from "./task-view-feed";
import type { Ev, FoldTable, TaskView } from "./task-view-types";

type MachineType =
  | "machine.created"
  | "machine.slept"
  | "machine.woke"
  | "machine.rebuilt"
  | "machine.reset"
  | "machine.control_claimed"
  | "machine.control_ended";

/** The newest control ending, kept so the screen can tell its holder who ended it. */
function controlEndedOf(v: TaskView, ev: Ev<MachineType>): TaskView["controlEnded"] {
  if (ev.type !== "machine.control_ended") return v.controlEnded;
  return {
    holderId: ev.actor?.id ?? "",
    endedBy: ev.ended_by ? { id: ev.ended_by.id, name: ev.ended_by.name } : null,
    reason: ev.reason,
    at: ev.at,
  };
}

function machineEvent(v: TaskView, ev: Ev<MachineType>): TaskView {
  return {
    ...v,
    feed: marker(
      v,
      "muted",
      "machine",
      machineMarkerText(ev, { profile: v.machineProfile, turn: v.machineTurn }),
    ),
    nextId: v.nextId + 1,
    machineVersion: v.machineVersion + 1,
    machineProfile:
      ev.type === "machine.created" || ev.type === "machine.woke" ? ev.profile : v.machineProfile,
    // the turn the Machine last worked in: a wake names its own, a creation is this turn's
    machineTurn:
      ev.type === "machine.woke" ? ev.turn : ev.type === "machine.created" ? v.turn : v.machineTurn,
    controlEnded: controlEndedOf(v, ev),
  };
}

/** Who drives the browser - state, not the thread (no marker; the durable
 *  machine.control_* rows draw the timeline). The newest frame is the truth: an ending said
 *  late carries the row's lease as it stood after it, never "nobody" over a newer claim. */
function screenState(v: TaskView, ev: Ev<"screen.state">): TaskView {
  return {
    ...v,
    screenVersion: v.screenVersion + 1,
    screen: ev.holder
      ? {
          holder: { id: ev.holder.id, name: ev.holder.name },
          private: ev.private ?? false,
          until: ev.until ?? null,
        }
      : null,
  };
}

/** Where the agent just acted, for the pane's flash. State, not the thread: the
 *  act's record is its own tool.called row. */
function screenAction(v: TaskView, ev: Ev<"screen.action">): TaskView {
  return {
    ...v,
    screenAction: { verb: ev.verb, box: ev.box ?? null, at: ev.at },
  };
}

export const MACHINE_FOLDS: FoldTable<MachineType | "screen.state" | "screen.action"> = {
  "machine.created": machineEvent,
  "machine.slept": machineEvent,
  "machine.woke": machineEvent,
  "machine.rebuilt": machineEvent,
  "machine.reset": machineEvent,
  "machine.control_claimed": machineEvent,
  "machine.control_ended": machineEvent,
  "screen.state": screenState,
  "screen.action": screenAction,
};
