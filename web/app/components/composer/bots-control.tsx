/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The Bot dial (v2 of this control) - the home composer's "this task is FOR this
// bot". Where v1 narrowed the roster (multi-toggle), the dial picks ONE: Auto keeps One
// routing across everything; naming a bot makes the task DIRECT - that bot answers every
// turn, deterministically, exactly like its own chat. Single-select radio grammar, the
// deliverable dial's shape.

import { BotIcon, Check } from "lucide-react";
import { Button } from "@makeplane/propel/components/button";
import { Button as ButtonElement } from "@makeplane/propel/elements/button";
import { Icon } from "@makeplane/propel/components/icon";
import { Popover, PopoverContent, PopoverTrigger } from "@makeplane/propel/components/popover";
import type { components } from "~/lib/api/types.gen";

export type BotOut = components["schemas"]["BotOut"];

export type DialBot = { name: string; description: string; ref: string; draft: boolean };

/** What the dial offers, one per name: the published head - or, when none exists, the
 *  author's own newest draft (the workbench law: drafts resolve only for their author, and a
 *  direct task is the DM's semantics, which already admits them). The ref is what the wire
 *  gets: the bare name pins the head; `name@draft` pins the author's draft. */
export const rosterHeads = (bots: BotOut[]): DialBot[] => {
  const byName = new Map<string, BotOut[]>();
  for (const b of bots) {
    const list = byName.get(b.name) ?? [];
    list.push(b);
    byName.set(b.name, list);
  }
  return [...byName.entries()]
    .map(([name, versions]) => {
      versions.sort((a, b) => b.version - a.version);
      const published = versions.find((v) => v.status === "published");
      const top = published ?? versions[0];
      if (!published && top.status !== "draft") return null; // archived-only: not offerable
      return {
        name,
        description: top.description,
        ref: published ? name : `${name}@draft`,
        draft: !published,
      };
    })
    .filter((x): x is DialBot => x !== null)
    .sort((a, b) => a.name.localeCompare(b.name));
};

export function BotsControl({
  bots,
  value,
  onChange,
}: {
  bots: BotOut[];
  value: string | null; // null = Auto - One routes
  onChange: (name: string | null) => void;
}) {
  const heads = rosterHeads(bots);
  // No published bots ⇒ no dial: authoring lives at /bots, and an empty picker dead-ends.
  if (heads.length === 0) return null;

  return (
    <Popover>
      {/* The element, not the ready-made: the face carries the picked name. A picked bot
          keeps a persistent fill (tertiary), the kit's set-state wash. */}
      <PopoverTrigger
        render={
          <ButtonElement variant={value !== null ? "tertiary" : "ghost"} size="md" stretch="auto" />
        }
        aria-label={value ? `Bot: ${value.replace(/@draft$/, "")}` : "Bot: Auto"}
        title="Bot: send this task to one of your own agents"
      >
        <Icon icon={BotIcon} />
        {value !== null && (
          <span className="max-w-28 truncate">{value.replace(/@draft$/, "")}</span>
        )}
      </PopoverTrigger>
      <PopoverContent align="start">
        <div className="w-72 space-y-1">
          <p className="text-muted-foreground px-2 pt-1 pb-1.5 text-caption-md-regular">
            Auto lets the planner route across everything. Naming a bot sends the task straight to
            it - only that bot answers, every turn.
          </p>
          <Row
            label="Auto"
            hint="One routes across built-ins and bots"
            selected={value === null}
            onPick={() => onChange(null)}
          />
          {heads.map((b) => (
            <Row
              key={b.name}
              label={b.draft ? `${b.name} (draft)` : b.name}
              hint={b.description}
              selected={value === b.ref}
              onPick={() => onChange(b.ref)}
            />
          ))}
          <div className="bg-border/60 mx-1 my-1.5 h-px" role="separator" />
          <Button
            variant="ghost"
            size="sm"
            stretch="full"
            label="Manage bots…"
            onClick={() => {
              window.location.assign("/bots");
            }}
          />
        </div>
      </PopoverContent>
    </Popover>
  );
}

function Row({
  label,
  hint,
  selected,
  onPick,
}: {
  label: string;
  hint: string;
  selected: boolean;
  onPick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onPick}
      className="hover:bg-accent/40 flex w-full items-start gap-2.5 rounded-md px-2 py-1.5 text-left transition-colors"
    >
      <span className="mt-0.5 w-4 shrink-0">{selected && <Check className="size-4" />}</span>
      <span className="min-w-0">
        <span className="block text-body-sm-regular">{label}</span>
        <span className="text-muted-foreground line-clamp-1 block text-caption-md-regular">
          {hint}
        </span>
      </span>
    </button>
  );
}
