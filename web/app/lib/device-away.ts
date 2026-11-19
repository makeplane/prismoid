/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// A step waiting for its device is a pause nobody asked for: its gate
// says so in its reason, and its title, "Waiting for <device name>", is what the page says.

/** The reason prefix of a PAUSE gate that waits for the task's device (`schemas/device_away.py`). */
export const DEVICE_AWAY = "device_away:";

export type DeviceWait = { gate_id: string; title: string } | null;

type PauseGate = { gate_id: string; reason?: string | null; title?: string | null };

function waitsForDevice(ev: Pick<PauseGate, "reason">): boolean {
  return (ev.reason ?? "").startsWith(DEVICE_AWAY);
}

export function deviceAwayOpened(wait: DeviceWait, ev: PauseGate): DeviceWait {
  if (!waitsForDevice(ev)) return wait;
  return { gate_id: ev.gate_id, title: ev.title || "Waiting for your computer" };
}

/** A lifted pause lifts the device wait; the read's seed ("") names no gate, so any lifted pause
 *  lifts it. */
export function deviceAwayLifted(wait: DeviceWait, gateId: string | undefined): DeviceWait {
  return wait && [gateId, ""].includes(wait.gate_id) ? null : wait;
}

/** The tree's one quiet line for a pause gate: a device wait says what it waits for, a person's
 *  pause says where it stopped. */
export function pauseSummary(ev: Omit<PauseGate, "gate_id">): string {
  return waitsForDevice(ev) && ev.title ? ev.title : "Paused before its next tool call";
}
