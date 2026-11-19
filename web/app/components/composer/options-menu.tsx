/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The Options menu: every setting of the task-minting box behind ONE button. Each row names a
// setting and its current value and opens that setting's choices as a submenu beside it, so the
// box's bottom row holds a handful of controls instead of a dozen. Nothing is decided here: the
// host owns every value, and the two settings that need typing (a custom schedule, a custom
// amount) hand off to their own panels through `onCustomWhen` / `onCustomBudget`.

import { Icon } from "@makeplane/propel/components/icon";
import {
  Menu,
  MenuCheckboxItem,
  MenuContent,
  MenuItem,
  MenuSeparator,
  MenuSubmenu,
  MenuSubmenuContent,
  MenuSubmenuTrigger,
  MenuTrigger,
} from "@makeplane/propel/components/menu";
import { Button as ButtonElement } from "@makeplane/propel/elements/button";
import {
  AlarmClock,
  BotIcon,
  Boxes,
  CircleDollarSign,
  HelpCircle,
  Layers,
  type LucideIcon,
  Plug,
  Ruler,
  Shapes,
  SlidersHorizontal,
  Sparkles,
} from "lucide-react";
import type { ReactNode } from "react";
import { Link } from "react-router";
import type { components } from "~/lib/api/types.gen";
import { rosterHeads, type BotOut } from "./bots-control";
import { attachable, type ConnectorOut } from "./connectors-control";
import {
  DELIVERABLES,
  SINGLE_BREATH_BLOCKED,
  SIZES,
  toggleFleetRef,
  type DeliverableChoice,
  type ModelOut,
  type TaskMode,
  type TaskScale,
} from "./dials";
import { BUDGET_PRESETS, CLARIFY_CHOICES, CLARIFY_COPY, type Clarify } from "./task-dials";
import { PRESETS, WHEN_NOW, whenLabel, type WhenValue } from "./when-control";

type SkillOut = components["schemas"]["SkillOut"];

const MODES: { value: TaskMode; label: string; hint: string }[] = [
  { value: "standard", label: "Standard", hint: "one model, routed to your ask" },
  { value: "fleet", label: "Fleet", hint: "every eligible model answers; a chair compares" },
];

export type OptionsMenuProps = {
  when: WhenValue;
  onWhen: (when: WhenValue) => void;
  /** Open the When panel: a typed schedule or an event cannot live in a menu row. */
  onCustomWhen: () => void;
  mode: TaskMode;
  onMode: (mode: TaskMode) => void;
  scale: TaskScale;
  onScale: (scale: TaskScale) => void;
  deliverable: DeliverableChoice;
  onDeliverable: (choice: DeliverableChoice) => void;
  fleetModels: ModelOut[];
  modelRefs: string[];
  onModelRefs: (refs: string[]) => void;
  clarify: Clarify;
  onClarify: (clarify: Clarify) => void;
  /** "" = no cap, else a normalized decimal string ("2.00"). */
  budgetUsd: string;
  onBudget: (usd: string) => void;
  /** Open the Budget panel on its amount field. */
  onCustomBudget: () => void;
  skills: SkillOut[];
  selectedSkills: string[];
  onToggleSkill: (name: string) => void;
  connectors: ConnectorOut[];
  connectorIds: string[];
  onConnectorIds: (ids: string[]) => void;
  bots: BotOut[];
  botRef: string | null;
  onBot: (ref: string | null) => void;
};

/** One setting: its row in the menu (icon, name, current value) and its choices beside it. */
function Setting({
  icon,
  label,
  value,
  children,
}: {
  icon: LucideIcon;
  label: string;
  value: string;
  children: ReactNode;
}) {
  return (
    <MenuSubmenu>
      <MenuSubmenuTrigger
        icon={<Icon icon={icon} tint="secondary" />}
        label={label}
        trailing={
          <span className="text-muted-foreground max-w-32 truncate text-caption-md-regular">
            {value}
          </span>
        }
      />
      <MenuSubmenuContent>{children}</MenuSubmenuContent>
    </MenuSubmenu>
  );
}

function WhenSetting({
  when,
  onWhen,
  onCustomWhen,
}: Pick<OptionsMenuProps, "when" | "onWhen" | "onCustomWhen">) {
  const preset = when.mode === "schedule" ? PRESETS.find((p) => p.cron === when.cron) : undefined;
  return (
    <Setting icon={AlarmClock} label="When" value={preset?.label ?? whenLabel(when)}>
      <MenuItem
        selected={when.mode === "now"}
        label="Now"
        description="send it once"
        onClick={() => onWhen(WHEN_NOW)}
      />
      <MenuSeparator />
      {PRESETS.map((p) => (
        <MenuItem
          key={p.cron}
          selected={preset?.cron === p.cron}
          label={p.label}
          onClick={() => onWhen({ mode: "schedule", cron: p.cron })}
        />
      ))}
      <MenuSeparator />
      <MenuItem
        label="Custom schedule or event…"
        description="saves this as a routine"
        onClick={onCustomWhen}
      />
    </Setting>
  );
}

function DeliverySettings({
  mode,
  onMode,
  scale,
  onScale,
  deliverable,
  onDeliverable,
}: Pick<
  OptionsMenuProps,
  "mode" | "onMode" | "scale" | "onScale" | "deliverable" | "onDeliverable"
>) {
  const fleet = mode === "fleet";
  return (
    <>
      <Setting icon={Layers} label="Mode" value={fleet ? "Fleet" : "Standard"}>
        {MODES.map((m) => (
          <MenuItem
            key={m.value}
            selected={mode === m.value}
            label={m.label}
            description={m.hint}
            onClick={() => onMode(m.value)}
          />
        ))}
      </Setting>
      <Setting
        icon={Ruler}
        label="Size"
        value={SIZES.find((s) => s.value === scale)?.label ?? scale}
      >
        {SIZES.map((s) => {
          const blocked = fleet && SINGLE_BREATH_BLOCKED.has(s.value);
          return (
            <MenuItem
              key={s.value}
              selected={scale === s.value}
              disabled={blocked}
              label={s.label}
              description={blocked ? "fleet answers are single-breath" : s.hint}
              onClick={() => onScale(s.value)}
            />
          );
        })}
      </Setting>
      {/* Only standard mode takes a deliverable: a fleet delivers the chair's table. */}
      {!fleet && (
        <Setting
          icon={Shapes}
          label="Deliverable"
          value={DELIVERABLES.find((d) => d.value === deliverable)?.label ?? deliverable}
        >
          {DELIVERABLES.map((d) => (
            <MenuItem
              key={d.value}
              selected={deliverable === d.value}
              icon={<Icon icon={d.icon} />}
              label={d.label}
              description={d.hint}
              onClick={() => onDeliverable(d.value)}
            />
          ))}
        </Setting>
      )}
    </>
  );
}

function FleetModelsSetting({
  fleetModels,
  modelRefs,
  onModelRefs,
}: Pick<OptionsMenuProps, "fleetModels" | "modelRefs" | "onModelRefs">) {
  return (
    <Setting
      icon={Boxes}
      label="Fleet models"
      value={`${modelRefs.length} of ${fleetModels.length}`}
    >
      {fleetModels.map((m) => (
        <MenuCheckboxItem
          key={m.ref}
          label={m.model_id}
          checked={modelRefs.includes(m.ref)}
          onCheckedChange={() => toggleFleetRef(modelRefs, m.ref, onModelRefs)}
        />
      ))}
    </Setting>
  );
}

function BudgetSetting({
  budgetUsd,
  onBudget,
  onCustomBudget,
}: Pick<OptionsMenuProps, "budgetUsd" | "onBudget" | "onCustomBudget">) {
  return (
    <Setting
      icon={CircleDollarSign}
      label="Budget"
      value={budgetUsd ? `≤ $${budgetUsd}` : "No cap"}
    >
      <MenuItem selected={budgetUsd === ""} label="No cap" onClick={() => onBudget("")} />
      {BUDGET_PRESETS.map((p) => (
        <MenuItem
          key={p}
          selected={budgetUsd === p.toFixed(2)}
          label={`$${p}`}
          onClick={() => onBudget(p.toFixed(2))}
        />
      ))}
      <MenuSeparator />
      <MenuItem label="Custom amount…" onClick={onCustomBudget} />
    </Setting>
  );
}

function ConnectorsSetting({
  connectors,
  connectorIds,
  onConnectorIds,
}: Pick<OptionsMenuProps, "connectors" | "connectorIds" | "onConnectorIds">) {
  const live = attachable(connectors);
  const parked = connectors.length - live.length;
  return (
    <Setting
      icon={Plug}
      label="Connectors"
      value={connectorIds.length ? `${connectorIds.length} attached` : "None"}
    >
      {live.map((c) => (
        <MenuCheckboxItem
          key={c.id}
          label={c.name}
          checked={connectorIds.includes(c.id)}
          onCheckedChange={(on) =>
            onConnectorIds(on ? [...connectorIds, c.id] : connectorIds.filter((x) => x !== c.id))
          }
        />
      ))}
      {live.length > 0 && <MenuSeparator />}
      <MenuItem label="Add connector…" render={<Link to="/connectors" />} />
      {connectors.length > 0 && (
        <MenuItem
          label="Manage connectors"
          secondaryText={parked > 0 ? `${parked} to authorize` : undefined}
          render={<Link to="/connectors" />}
        />
      )}
    </Setting>
  );
}

function BotSetting({ bots, botRef, onBot }: Pick<OptionsMenuProps, "bots" | "botRef" | "onBot">) {
  const heads = rosterHeads(bots);
  // No offerable bot, no row: authoring lives at /bots, and an empty picker dead-ends.
  if (heads.length === 0) return null;
  return (
    <Setting icon={BotIcon} label="Bot" value={botRef ? botRef.replace(/@draft$/, "") : "Auto"}>
      <MenuItem
        selected={botRef === null}
        label="Auto"
        description="One routes across built-ins and bots"
        onClick={() => onBot(null)}
      />
      {heads.map((b) => (
        <MenuItem
          key={b.name}
          selected={botRef === b.ref}
          label={b.draft ? `${b.name} (draft)` : b.name}
          description={b.description || undefined}
          onClick={() => onBot(b.ref)}
        />
      ))}
      <MenuSeparator />
      <MenuItem label="Manage bots…" render={<Link to="/bots" />} />
    </Setting>
  );
}

export function OptionsMenu(p: OptionsMenuProps) {
  return (
    <Menu modal={false}>
      <MenuTrigger
        aria-label="Options"
        title="Options: when it runs, mode, size, deliverable, budget and more"
        render={<ButtonElement variant="ghost" size="md" stretch="auto" />}
      >
        <Icon icon={SlidersHorizontal} />
        <span>Options</span>
      </MenuTrigger>
      <MenuContent align="start">
        <WhenSetting when={p.when} onWhen={p.onWhen} onCustomWhen={p.onCustomWhen} />
        <DeliverySettings
          mode={p.mode}
          onMode={p.onMode}
          scale={p.scale}
          onScale={p.onScale}
          deliverable={p.deliverable}
          onDeliverable={p.onDeliverable}
        />
        {p.mode === "fleet" && p.fleetModels.length > 0 && (
          <FleetModelsSetting
            fleetModels={p.fleetModels}
            modelRefs={p.modelRefs}
            onModelRefs={p.onModelRefs}
          />
        )}
        <MenuSeparator />
        <Setting
          icon={HelpCircle}
          label="Clarifying questions"
          value={CLARIFY_COPY[p.clarify].label}
        >
          {CLARIFY_CHOICES.map((c) => (
            <MenuItem
              key={c}
              selected={p.clarify === c}
              label={CLARIFY_COPY[c].label}
              description={CLARIFY_COPY[c].hint}
              onClick={() => p.onClarify(c)}
            />
          ))}
        </Setting>
        <BudgetSetting
          budgetUsd={p.budgetUsd}
          onBudget={p.onBudget}
          onCustomBudget={p.onCustomBudget}
        />
        <MenuSeparator />
        {p.skills.length > 0 && (
          <Setting
            icon={Sparkles}
            label="Skills"
            value={p.selectedSkills.length ? `${p.selectedSkills.length} pinned` : "None"}
          >
            {p.skills.map((s) => (
              <MenuCheckboxItem
                key={s.name}
                label={s.name}
                checked={p.selectedSkills.includes(s.name)}
                onCheckedChange={() => p.onToggleSkill(s.name)}
              />
            ))}
          </Setting>
        )}
        <ConnectorsSetting
          connectors={p.connectors}
          connectorIds={p.connectorIds}
          onConnectorIds={p.onConnectorIds}
        />
        <BotSetting bots={p.bots} botRef={p.botRef} onBot={p.onBot} />
      </MenuContent>
    </Menu>
  );
}
