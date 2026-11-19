/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The three dials only the task-minting box carries (Clarify, Budget, Skills), each one pill and
// its own panel. The box shows a pill only while its value is off the default (the Options menu
// is where every setting starts), so each stays a small controlled component the box can mount
// and unmount freely.

import { Icon } from "@makeplane/propel/components/icon";
import { Input, InputGroup } from "@makeplane/propel/components/input";
import { Popover, PopoverContent, PopoverTrigger } from "@makeplane/propel/components/popover";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectList,
  SelectTrigger,
} from "@makeplane/propel/components/select";
import { Button as ButtonElement } from "@makeplane/propel/elements/button";
import { Check, CircleDollarSign, HelpCircle, Sparkles } from "lucide-react";
import { useState } from "react";
import { ToggleChip } from "~/components/kit";
import type { components } from "~/lib/api/types.gen";

type SkillOut = components["schemas"]["SkillOut"];

export const MAX_SKILLS = 8;

export type Clarify = "auto" | "never" | "always";

/** One vocabulary for the Clarify setting: the pill, its panel and the Options menu read it. */
export const CLARIFY_COPY: Record<Clarify, { label: string; hint: string }> = {
  auto: { label: "Auto", hint: "asks first only when your ask is ambiguous" },
  never: { label: "Never", hint: "starts immediately on sensible defaults" },
  always: { label: "Always", hint: "one round of questions, every task" },
};
export const CLARIFY_CHOICES = Object.keys(CLARIFY_COPY) as Clarify[];

// The Clarify trigger's face: the icon alone for the default, the word otherwise.
const CLARIFY_TRIGGER = {
  auto: <span className="sr-only">Clarify: auto</span>,
  never: "never",
  always: "always",
};

export function ClarifyDial({
  value,
  onChange,
}: {
  value: Clarify;
  onChange: (value: Clarify) => void;
}) {
  return (
    <Select
      modal={false}
      value={value}
      onValueChange={(v) => v && onChange(v as Clarify)}
      items={CLARIFY_TRIGGER}
    >
      <SelectTrigger
        size="md"
        variant="ghost"
        width="fit"
        icon={<Icon icon={HelpCircle} />}
        aria-label="Clarifying questions"
        title="Clarifying questions - when to ask before starting"
      />
      <SelectContent align="start" sizing="auto">
        <SelectList>
          {CLARIFY_CHOICES.map((c) => (
            <SelectItem
              key={c}
              value={c}
              label={`Clarify: ${c}`}
              description={CLARIFY_COPY[c].hint}
            />
          ))}
        </SelectList>
      </SelectContent>
    </Select>
  );
}

export const BUDGET_PRESETS = [1, 2, 5, 10];

/** A typed amount as the committed budget: any positive number becomes "X.XX"; anything else
 *  (empty, zero, junk) means no cap. */
export function normalizeBudget(draft: string): string {
  const n = Number(draft.trim());
  return Number.isFinite(n) && n > 0 ? n.toFixed(2) : "";
}

/** Budget as a pill + popover: the pill states its value ("≤ $2.00"); the panel is where the
 *  meaning, the format, and the fast paths live. The value is a normalized decimal string or ""
 *  for no cap; the panel edits a draft so half-typed numbers never leak into the pill or the
 *  payload. `defaultOpen` mounts it with the panel open (the Options menu's "Custom amount"). */
export function BudgetDial({
  value,
  onChange,
  defaultOpen = false,
  onOpenChange,
}: {
  value: string;
  onChange: (value: string) => void;
  defaultOpen?: boolean;
  onOpenChange?: (open: boolean) => void;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const [draft, setDraft] = useState(value);
  function close(next: string) {
    onChange(normalizeBudget(next));
    setOpen(false);
    onOpenChange?.(false);
  }
  return (
    <Popover
      open={open}
      onOpenChange={(o) => {
        if (!o) return close(draft);
        setDraft(value);
        setOpen(true);
        onOpenChange?.(true);
      }}
    >
      <PopoverTrigger
        render={<ButtonElement variant={value ? "tertiary" : "ghost"} size="md" stretch="auto" />}
        aria-label="Budget - a USD spend cap for this task"
        title="Budget - a USD spend cap for this task"
      >
        <Icon icon={CircleDollarSign} />
        {value ? (
          <span className="tabular-nums">≤ ${value}</span>
        ) : (
          <span className="sr-only">Budget</span>
        )}
      </PopoverTrigger>
      <PopoverContent align="start">
        <div className="w-72 space-y-3">
          <div className="space-y-1">
            <p className="text-body-sm-regular font-medium">Spend cap</p>
            <p className="text-muted-foreground text-caption-md-regular">
              A hard ceiling for this task, in USD. Work stops cleanly rather than exceed it. Leave
              empty for no cap.
            </p>
          </div>
          <InputGroup size="md">
            <span className="text-muted-foreground">$</span>
            <Input
              size="md"
              autoFocus
              inputMode="decimal"
              placeholder="2.00"
              aria-label="Spend cap in USD"
              value={draft}
              onChange={(e) => setDraft(e.currentTarget.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") close(draft);
              }}
            />
          </InputGroup>
          <div className="flex items-center gap-1.5">
            {BUDGET_PRESETS.map((p) => (
              <ToggleChip key={p} onClick={() => close(String(p))}>
                ${p}
              </ToggleChip>
            ))}
            {value && (
              <button
                type="button"
                onClick={() => close("")}
                className="text-muted-foreground hover:text-foreground ml-auto text-caption-md-regular transition-colors"
              >
                Clear
              </button>
            )}
          </div>
        </div>
      </PopoverContent>
    </Popover>
  );
}

export function SkillsDial({
  skills,
  selected,
  onToggle,
}: {
  skills: SkillOut[];
  selected: string[];
  onToggle: (name: string) => void;
}) {
  return (
    <Popover>
      <PopoverTrigger
        render={
          <ButtonElement
            variant={selected.length > 0 ? "tertiary" : "ghost"}
            size="md"
            stretch="auto"
          />
        }
        aria-label="Skills"
        title="Skills - pin playbooks for this task"
      >
        <Icon icon={Sparkles} />
        <span className="sr-only">Skills</span>
        {selected.length > 0 && <span className="tabular-nums">{selected.length}</span>}
      </PopoverTrigger>
      <PopoverContent align="start">
        <div className="w-72 space-y-2.5">
          <p className="text-muted-foreground text-caption-md-regular">
            Pin skills for this task
            {selected.length > 0 && ` · ${selected.length}/${MAX_SKILLS}`}
          </p>
          <div className="flex flex-wrap gap-1.5">
            {skills.map((s) => {
              const on = selected.includes(s.name);
              return (
                <ToggleChip
                  key={s.name}
                  active={on}
                  title={s.description}
                  onClick={() => onToggle(s.name)}
                >
                  {on && <Check className="size-3.5" />}
                  {s.name}
                </ToggleChip>
              );
            })}
          </div>
        </div>
      </PopoverContent>
    </Popover>
  );
}
