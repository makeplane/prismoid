/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The control dock (ADR-065): a CONTROL gate's face in the transcript. The screen
// pane's banner is the main surface; this one line is how someone without the pane open finds
// it - 'The agent needs someone to "sign in to …"' (the agent asked, in its quoted words:
// `source: "handoff"`) or "Ada took control of the browser" (a person grabbed it mid-step:
// `source: "takeover"`).
// Nobody accepts a CONTROL gate by hand (the server answers 409): a hand-back answers it, so the
// only verbs are Open the screen and, on a handoff, Skip (a decline: the agent carries on).
// On a device (`gate.machine_driver === "device"`) there is no screen:
// the browser is a window on the creator's own computer, and THEIR accept - Done - hands it back.

import { Button } from "@makeplane/propel/components/button";
import { MonitorPlay } from "lucide-react";
import { useState } from "react";
import { api, toastError, unwrap } from "~/lib/api/client";
import { browserOn } from "~/lib/machine-copy";
import type { GateView } from "~/lib/task-view";
import { cn } from "~/lib/utils";
import { DOCK_SHELL } from "./approval-dock";

/** The gate's one line: its title (the server writes the sentence), else its reason. */
export function controlLine(gate: Pick<GateView, "title" | "reason" | "source">): string {
  return (
    gate.title?.trim() ||
    gate.reason?.trim() ||
    (gate.source === "handoff"
      ? "The agent needs someone to take over the browser"
      : "Someone took control of the browser")
  );
}

/** Skip: decline the handoff gate through the ordinary resolve route (an editor's verb). The
 *  parked call then answers "Nobody took over (… skipped)". Shared with the pane's banner. */
export async function skipHandoff(gateId: string): Promise<void> {
  await unwrap(
    api.POST("/v1/gates/{gate_id}/resolve", {
      params: { path: { gate_id: gateId } },
      body: { action: "decline" },
    }),
  );
}

/** Done: the task's creator hands a device's browser window back -
 *  the accept the server takes from them on a device task only. The parked call then answers
 *  "{name} took over and handed back after …". */
export async function doneHandoff(gateId: string): Promise<void> {
  await unwrap(
    api.POST("/v1/gates/{gate_id}/resolve", {
      params: { path: { gate_id: gateId } },
      body: { action: "accept" },
    }),
  );
}

export function ControlDock({
  gate,
  canEdit,
  isCreator = false,
  deviceName = null,
  onOpenScreen,
}: {
  gate: GateView;
  /** A Project editor (or a personal task's creator) may Skip; a viewer only opens the screen. */
  canEdit: boolean;
  /** The task's creator: on a device, the only one who says Done. */
  isCreator?: boolean;
  /** The device's name, for "Browser on …" (null: "your computer"). */
  deviceName?: string | null;
  onOpenScreen: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const handoff = gate.source === "handoff";
  const device = gate.machine_driver === "device";

  async function act(run: (id: string) => Promise<void>, failed: string) {
    if (busy) return;
    setBusy(true);
    try {
      await run(gate.gate_id);
    } catch (e) {
      toastError(e, failed);
    } finally {
      setBusy(false); // the key-remount law: never leave the latch set
    }
  }

  return (
    <div className={cn(DOCK_SHELL, "flex items-center gap-2")}>
      <span className="bg-muted text-muted-foreground flex size-7 shrink-0 items-center justify-center rounded-md">
        <MonitorPlay className="size-3.5" />
      </span>
      {device ? (
        <div className="min-w-0 flex-1">
          <p className="text-body-sm-regular break-words">{controlLine(gate)}</p>
          <p className="text-muted-foreground text-caption-md-regular">
            {browserOn(deviceName)}: do it in its Prismoid Chrome window, then say Done.
          </p>
        </div>
      ) : (
        <p className="min-w-0 flex-1 text-body-sm-regular break-words">{controlLine(gate)}</p>
      )}
      {handoff && canEdit && (
        <Button
          variant="secondary"
          size="sm"
          stretch="auto"
          disabled={busy}
          onClick={() => void act(skipHandoff, "Couldn't skip it.")}
          label="Skip"
        />
      )}
      {device ? (
        isCreator && (
          <Button
            variant="primary"
            size="sm"
            stretch="auto"
            disabled={busy}
            onClick={() => void act(doneHandoff, "Couldn't hand it back.")}
            label="Done"
          />
        )
      ) : (
        <Button
          variant="primary"
          size="sm"
          stretch="auto"
          onClick={onOpenScreen}
          label="Open the screen"
        />
      )}
    </div>
  );
}
