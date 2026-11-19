/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// Past the Options button the task-minting box's row holds only what was CHANGED: a setting off
// its default wears its own pill (the same control it always was, so a click edits it in place)
// with a clear button beside it. At rest this renders nothing and the row is Project, Options,
// mic, send. It reads the very props the Options menu reads: one set of values, two faces.

import { X } from "lucide-react";
import type { ReactNode } from "react";
import { BotsControl } from "./bots-control";
import { ConnectorsControl } from "./connectors-control";
import { DeliverableDial, FleetModelsDial, ModeDial, SizeDial } from "./dials";
import type { OptionsMenuProps } from "./options-menu";
import { BudgetDial, ClarifyDial, SkillsDial } from "./task-dials";
import { WHEN_NOW, WhenDial } from "./when-control";

/** The one panel the Options menu can ask the row to open: a custom schedule and a custom amount
 *  need a text field, which a menu row cannot hold. One value, never two booleans. */
export type Reveal = "when" | "budget" | null;

export type SetOptionsProps = OptionsMenuProps & {
  reveal: Reveal;
  /** How many times each panel has been asked for. A pill's key: asking again remounts a pill
   *  that is already in the row, so its panel opens; closing changes nothing, so the pill (and
   *  the focus that returns to it) stays put. */
  revealed: Record<NonNullable<Reveal>, number>;
  /** The revealed panel closed (with or without a value). */
  onRevealDone: () => void;
  onClearSkills: () => void;
};

/** One changed setting: its pill and a clear button. `set` is false while a panel is open on a
 *  value not chosen yet (nothing to clear). */
function SetOption({
  name,
  onClear,
  set = true,
  children,
}: {
  name: string;
  onClear: () => void;
  set?: boolean;
  children: ReactNode;
}) {
  return (
    <span className="flex items-center">
      {children}
      {set && (
        <button
          type="button"
          aria-label={`Clear ${name}`}
          title={`Clear ${name}`}
          onClick={onClear}
          className="text-muted-foreground hover:text-foreground hover:bg-accent/50 grid size-5 place-items-center rounded-sm transition-colors"
        >
          <X className="size-3" />
        </button>
      )}
    </span>
  );
}

/** How the task runs: when, mode, fleet seats, size, deliverable. */
function RunOptions(p: SetOptionsProps) {
  const fleet = p.mode === "fleet";
  const closed = (open: boolean) => !open && p.onRevealDone();
  return (
    <>
      {(p.when.mode !== "now" || p.reveal === "when") && (
        <SetOption name="schedule" onClear={() => p.onWhen(WHEN_NOW)} set={p.when.mode !== "now"}>
          <WhenDial
            key={p.revealed.when}
            value={p.when}
            onChange={p.onWhen}
            defaultOpen={p.reveal === "when"}
            onOpenChange={closed}
          />
        </SetOption>
      )}
      {fleet && (
        <SetOption name="fleet mode" onClear={() => p.onMode("standard")}>
          <ModeDial mode={p.mode} onChange={p.onMode} />
        </SetOption>
      )}
      {fleet && p.modelRefs.length < p.fleetModels.length && (
        <SetOption
          name="fleet models"
          onClear={() => p.onModelRefs(p.fleetModels.map((m) => m.ref))}
        >
          <FleetModelsDial models={p.fleetModels} refs={p.modelRefs} onChange={p.onModelRefs} />
        </SetOption>
      )}
      {p.scale !== "auto" && (
        <SetOption name="size" onClear={() => p.onScale("auto")}>
          <SizeDial scale={p.scale} onChange={p.onScale} fleet={fleet} />
        </SetOption>
      )}
      {/* Only standard mode takes a deliverable (a fleet delivers the chair's table): choosing
          a kind constrains the plan end to end. */}
      {!fleet && p.deliverable !== "auto" && (
        <SetOption name="deliverable" onClear={() => p.onDeliverable("auto")}>
          <DeliverableDial value={p.deliverable} onChange={p.onDeliverable} />
        </SetOption>
      )}
    </>
  );
}

/** What the task may ask, spend and reach: clarify, budget, skills, connectors, bot. */
function ReachOptions(p: SetOptionsProps) {
  return (
    <>
      {p.clarify !== "auto" && (
        <SetOption name="clarifying questions" onClear={() => p.onClarify("auto")}>
          <ClarifyDial value={p.clarify} onChange={p.onClarify} />
        </SetOption>
      )}
      {(p.budgetUsd !== "" || p.reveal === "budget") && (
        <SetOption name="budget" onClear={() => p.onBudget("")} set={p.budgetUsd !== ""}>
          <BudgetDial
            key={p.revealed.budget}
            value={p.budgetUsd}
            onChange={p.onBudget}
            defaultOpen={p.reveal === "budget"}
            onOpenChange={(open) => !open && p.onRevealDone()}
          />
        </SetOption>
      )}
      {p.selectedSkills.length > 0 && (
        <SetOption name="skills" onClear={p.onClearSkills}>
          <SkillsDial skills={p.skills} selected={p.selectedSkills} onToggle={p.onToggleSkill} />
        </SetOption>
      )}
      {p.connectorIds.length > 0 && (
        <SetOption name="connectors" onClear={() => p.onConnectorIds([])}>
          <ConnectorsControl
            connectors={p.connectors}
            ids={p.connectorIds}
            onChange={p.onConnectorIds}
          />
        </SetOption>
      )}
      {p.botRef !== null && (
        <SetOption name="bot" onClear={() => p.onBot(null)}>
          <BotsControl bots={p.bots} value={p.botRef} onChange={p.onBot} />
        </SetOption>
      )}
    </>
  );
}

export function SetOptions(p: SetOptionsProps) {
  return (
    <>
      <RunOptions {...p} />
      <ReachOptions {...p} />
    </>
  );
}
