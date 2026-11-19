/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useMemo, useState } from "react";
import { HUE_BG, HUE_COUNT, SearchBox, ToggleChip } from "~/components/kit";
import { cn } from "~/lib/utils";
import { ICON_CATALOG } from "./catalog";
import { EMOJI_CATALOG } from "./emoji";
import { formatIcon } from "./value";

/** The face picker - content only, no popover of its own, so it mounts wherever the
 *  consumer wants (a Popover today, a sheet tomorrow). Perplexity's grammar: Icons/Emoji
 *  tabs, search, the hue row, the grid. Picks are PARTIAL - choosing a glyph doesn't touch
 *  the hue and vice versa - and `null` means "back to default" (the consumer maps it onto
 *  the API's clear flags). The picker owns no server state: give it the current face,
 *  it hands back intents. */
export function IconPicker({
  icon,
  hue,
  onPick,
}: {
  icon: string | null;
  hue: number | null;
  onPick: (next: { icon?: string | null; hue?: number | null }) => void;
}) {
  const [tab, setTab] = useState<"icons" | "emoji">("icons");
  const [q, setQ] = useState("");
  const needle = q.trim().toLowerCase();

  const icons = useMemo(
    () => (needle ? ICON_CATALOG.filter((e) => e.keywords.includes(needle)) : ICON_CATALOG),
    [needle],
  );
  const emoji = useMemo(
    () => (needle ? EMOJI_CATALOG.filter((e) => e.keywords.includes(needle)) : EMOJI_CATALOG),
    [needle],
  );

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-1.5">
        <ToggleChip active={tab === "icons"} onClick={() => setTab("icons")}>
          Icons
        </ToggleChip>
        <ToggleChip active={tab === "emoji"} onClick={() => setTab("emoji")}>
          Emoji
        </ToggleChip>
        {icon !== null && (
          <button
            onClick={() => onPick({ icon: null, hue: null })}
            className="text-muted-foreground hover:text-foreground ml-auto text-caption-md-regular transition-colors"
          >
            Reset
          </button>
        )}
      </div>

      <SearchBox
        value={q}
        onChange={setQ}
        placeholder={tab === "icons" ? "Search icons…" : "Search emoji…"}
        className="max-w-full"
      />

      {/* the hue row: neutral first, then the categorical ramp - the same 8 hues user data
          wears everywhere else, so a project's color can never clash with the system */}
      <div className="flex items-center gap-1.5" role="radiogroup" aria-label="Icon color">
        <HueSwatch
          selected={hue === null}
          onClick={() => onPick({ hue: null })}
          label="Neutral"
          className="bg-muted-foreground/40"
        />
        {Array.from({ length: HUE_COUNT }, (_, i) => (
          <HueSwatch
            key={i}
            selected={hue === i}
            onClick={() => onPick({ hue: i })}
            label={`Color ${i + 1}`}
            className={HUE_BG[i]}
          />
        ))}
      </div>

      <div className="scrollbar-thin grid max-h-56 grid-cols-8 gap-1 overflow-y-auto">
        {tab === "icons"
          ? icons.map(({ slug, icon: Glyph }) => (
              <GridCell
                key={slug}
                label={slug.replace(/-/g, " ")}
                selected={icon === `lucide:${slug}`}
                onClick={() => onPick({ icon: formatIcon({ kind: "lucide", name: slug }) })}
              >
                <Glyph className="text-muted-foreground size-4" />
              </GridCell>
            ))
          : emoji.map(({ char, keywords }) => (
              <GridCell
                key={char}
                label={keywords.split(" ")[0] ?? char}
                selected={icon === `emoji:${char}`}
                onClick={() => onPick({ icon: formatIcon({ kind: "emoji", char }) })}
              >
                <span className="text-h5-regular leading-none">{char}</span>
              </GridCell>
            ))}
        {(tab === "icons" ? icons : emoji).length === 0 && (
          <p className="text-muted-foreground col-span-full py-6 text-center text-caption-md-regular">
            No matches for “{q.trim()}”
          </p>
        )}
      </div>
    </div>
  );
}

function HueSwatch({
  selected,
  onClick,
  label,
  className,
}: {
  selected: boolean;
  onClick: () => void;
  label: string;
  className: string;
}) {
  return (
    <button
      role="radio"
      aria-checked={selected}
      aria-label={label}
      onClick={onClick}
      className={cn(
        "size-6 rounded-sm transition-shadow",
        selected && "ring-ring ring-2 ring-offset-1 ring-offset-background",
        className,
      )}
    />
  );
}

function GridCell({
  children,
  label,
  selected,
  onClick,
}: {
  children: React.ReactNode;
  label: string;
  selected: boolean;
  onClick: () => void;
}) {
  return (
    <button
      aria-label={label}
      aria-pressed={selected}
      onClick={onClick}
      className={cn(
        "hover:bg-accent flex size-8 items-center justify-center rounded-md transition-colors",
        selected && "bg-accent ring-ring ring-1",
      )}
    >
      {children}
    </button>
  );
}
