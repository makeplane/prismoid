/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The CONTROL gate's dock as the task page draws it (ADR-065): on a device Machine the gate names
// its driver, so the creator's Done replaces the screen. Composes the
// dock with the machine facts the task's events never carry.

import { ControlDock } from "~/components/tasks/control-dock";
import { controlGateFor } from "~/lib/machine-copy";
import type { GateView } from "~/lib/task-view";

export function MachineControlDock({
  gate,
  machine,
  canEdit,
  isCreator,
  onOpenScreen,
}: {
  gate: GateView;
  machine: { driver?: string | null; device_name?: string | null } | null | undefined;
  canEdit: boolean;
  isCreator: boolean;
  onOpenScreen: () => void;
}) {
  return (
    <ControlDock
      gate={controlGateFor(gate, machine)}
      canEdit={canEdit}
      isCreator={isCreator}
      deviceName={machine?.device_name}
      onOpenScreen={onOpenScreen}
    />
  );
}
