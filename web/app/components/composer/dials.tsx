/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { Button } from "@makeplane/propel/components/button";
import { Icon } from "@makeplane/propel/components/icon";
import {
  Menu,
  MenuContent,
  MenuItem,
  MenuSeparator,
  MenuSubmenu,
  MenuSubmenuContent,
  MenuSubmenuTrigger,
  MenuTrigger,
} from "@makeplane/propel/components/menu";
import { Popover, PopoverContent, PopoverTrigger } from "@makeplane/propel/components/popover";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectList,
  SelectTrigger,
} from "@makeplane/propel/components/select";
import { Button as ButtonElement } from "@makeplane/propel/elements/button";
import { Boxes, Check, ChevronsUpDown, Layers, Ruler, Shapes } from "lucide-react";
import { useMemo, type ReactNode } from "react";
import { toast } from "@prismoid/blocks/toast";
import { artifactIcon, artifactKindLabel } from "~/components/kit";
import type { components } from "~/lib/api/types.gen";
import { cn } from "~/lib/utils";

export type ModelOut = components["schemas"]["ModelOut"];
export type TaskMode = "standard" | "fleet";

// Mirrors the backend's EXECUTOR_CLASSES: only a model that can author a document holds a
// fleet seat - judge- and vision-only rows never appear in the picker.
export const EXECUTOR_CLASSES = new Set([
  "frontier-reasoner",
  "fast-executor",
  "frontier-executor",
  "long-context",
  "code",
]);

export const fleetEligible = (models: ModelOut[]) =>
  models.filter((m) => m.classes.some((c) => EXECUTOR_CLASSES.has(c)));

/** The Mode dial - Standard / Fleet, one ghost pill. Shared by every composer that mints a
 *  task (the landing box, the task page's follow-up). */
// The bar's word law: a label appears only when it carries a CHOSEN value - the default state
// is the icon alone (tooltip + sr-only keep it named). Seven worded pills overflowed the ring;
// a quiet icon strip cannot. The trigger shows these, not the menu rows.
const MODE_VALUE: Record<TaskMode, ReactNode> = {
  standard: <span className="sr-only">Standard</span>,
  fleet: "Fleet",
};

export function ModeDial({
  mode,
  onChange,
}: {
  mode: TaskMode;
  onChange: (mode: TaskMode) => void;
}) {
  return (
    // Non-modal, as the kit's select was: the page stays scrollable, an outside press lands.
    <Select
      modal={false}
      items={MODE_VALUE}
      value={mode}
      onValueChange={(v) => v && onChange(v as TaskMode)}
    >
      <SelectTrigger
        size="md"
        variant="ghost"
        width="fit"
        icon={<Icon icon={Layers} />}
        aria-label="Mode"
        title="Mode: standard or fleet"
      />
      <SelectContent sizing="auto">
        <SelectList>
          <SelectItem
            value="standard"
            label="Standard"
            description="one model, routed to your ask"
          />
          <SelectItem
            value="fleet"
            label="Fleet"
            description="every eligible model answers; a chair compares"
          />
        </SelectList>
      </SelectContent>
    </Select>
  );
}

// Derived from the GENERATED union, never hand-retyped (review): removing a bucket
// server-side fails assignability, and the Record below fails tsc until a NEW bucket is
// named here too - drift is loud in both directions.
export type TaskScale = NonNullable<components["schemas"]["TaskCreateIn"]["scale"]>;

// One vocabulary, five sizes, every artifact kind. Auto is the promise that one of
// the five is chosen from the goal's own numbers - or standard, never bigger (the no-push
// law). Hints quote the size table's own bands (schemas/sizing.py) - the five surfaces can
// never disagree, and "25–65" once did (review).
const SIZE_COPY: Record<TaskScale, { label: string; hint: string }> = {
  auto: { label: "Auto", hint: "a number in the goal decides; otherwise standard" },
  brief: { label: "Brief", hint: "a memo, ~5–8 slides, one table" },
  standard: { label: "Standard", hint: "a normal document, talk deck, workbook" },
  extended: { label: "Extended", hint: "10–25 pages, a seminar, a real model" },
  long: { label: "Long", hint: "30–60 pages, a workshop, a scenario model" },
  book: { label: "Book", hint: "a monograph, a course, a full model" },
};
export const SIZES = (Object.keys(SIZE_COPY) as TaskScale[]).map((value) => ({
  value,
  ...SIZE_COPY[value],
}));

// The trigger's word law, as Mode's: Auto is the icon alone.
const SIZE_VALUE = Object.fromEntries(
  SIZES.map((s) => [s.value, s.value === "auto" ? <span className="sr-only">Auto</span> : s.label]),
) as Record<TaskScale, ReactNode>;

/** Fleet is single-breath: the server refuses these buckets for fleet tasks, so
 *  the composer never offers the pair - the dial disables, the mode-switch prunes. */
export const SINGLE_BREATH_BLOCKED: ReadonlySet<TaskScale> = new Set(["extended", "long", "book"]);

/** The Size dial - Auto + the five buckets, one ghost pill; same word law as Mode:
 *  the icon alone until a non-default is CHOSEN. */
export function SizeDial({
  scale,
  onChange,
  fleet = false,
}: {
  scale: TaskScale;
  onChange: (scale: TaskScale) => void;
  /** Fleet mode disables the big three with the single-breath hint instead of a 422. */
  fleet?: boolean;
}) {
  return (
    <Select
      modal={false}
      items={SIZE_VALUE}
      value={scale}
      onValueChange={(v) => v && onChange(v as TaskScale)}
    >
      <SelectTrigger
        size="md"
        variant="ghost"
        width="fit"
        icon={<Icon icon={Ruler} />}
        aria-label="Size"
        title="Size: auto, brief, standard, extended, long, book"
      />
      <SelectContent sizing="auto">
        <SelectList>
          {SIZES.map((s) => {
            const blocked = fleet && SINGLE_BREATH_BLOCKED.has(s.value);
            return (
              // Label with the hint stacked beneath - the right-aligned squeeze read as clutter.
              <SelectItem
                key={s.value}
                value={s.value}
                disabled={blocked}
                label={s.label}
                description={blocked ? "fleet answers are single-breath" : s.hint}
              />
            );
          })}
        </SelectList>
      </SelectContent>
    </Select>
  );
}

// Derived from the GENERATED union like TaskScale (drift loud both directions). The wire
// has NO "auto" - absence IS auto - and the kind and its document FORMAT travel as two
// fields; the DIAL is ONE control: a single menu where the
// document rows carry their format ("Document · PDF"), because two side-by-side dropdowns
// read as clutter. `splitDeliverableChoice` turns a row back into the wire's two fields.
export type DeliverableKind = NonNullable<components["schemas"]["TaskCreateIn"]["deliverable"]>;
export type DocumentFormat = NonNullable<
  components["schemas"]["TaskCreateIn"]["deliverable_format"]
>;
export type DeliverableChoice = DeliverableKind | "auto" | `document:${DocumentFormat}`;

export const FORMAT_LABEL: Record<DocumentFormat, string> = {
  md: "Markdown",
  docx: "Word",
  pdf: "PDF",
  html: "HTML page",
};

// One menu, six top rows - the document family NESTS (the
// flat "Document / Document · Markdown / Document · Word / …" run read as five sibling
// kinds). Labels compose from the kit vocabulary + FORMAT_LABEL; the Record over the
// union keeps drift loud when either wire field grows.
const DELIVERABLE_HINTS: Record<DeliverableChoice, string> = {
  auto: "your words decide; the chip shows the path",
  document: "a written piece - your words pick the format",
  "document:md": "plain prose (.md) - reads in the app",
  "document:docx": "a real Word file (.docx)",
  "document:pdf": "a typeset PDF",
  "document:html": "one self-contained page (.html)",
  collection: "structured rows - table, board, timeline",
  spreadsheet: "a real workbook (.xlsx)",
  deck: "slides (.pptx)",
  code: "a project archive",
};
const DOCUMENT_FORMAT_CHOICES = [
  "document:md",
  "document:docx",
  "document:pdf",
  "document:html",
] as const;
export const DELIVERABLES = (Object.keys(DELIVERABLE_HINTS) as DeliverableChoice[]).map(
  (value) => ({
    value,
    label:
      value === "auto"
        ? "Auto"
        : value.startsWith("document:")
          ? `Document · ${FORMAT_LABEL[value.slice("document:".length) as DocumentFormat]}`
          : artifactKindLabel(value),
    hint: DELIVERABLE_HINTS[value],
    // Each row wears its kind's glyph (the one vocabulary home); Auto wears the dial's own.
    icon:
      value === "auto" ? Shapes : artifactIcon(value.startsWith("document") ? "document" : value),
  }),
);

/** A dial row → the wire's two fields. "auto" = neither (absence is auto). */
export function splitDeliverableChoice(choice: DeliverableChoice): {
  deliverable?: DeliverableKind;
  deliverable_format?: DocumentFormat;
} {
  if (choice === "auto") return {};
  if (choice.startsWith("document:")) {
    return {
      deliverable: "document",
      deliverable_format: choice.slice("document:".length) as DocumentFormat,
    };
  }
  return { deliverable: choice as DeliverableKind };
}

/** One row of the deliverable menu: Propel's single-select row (`selected` on every row makes
 *  it a menuitemradio with a trailing check), the hint as its second line. A plain item closes
 *  the menu on click, so every row finishes the gesture as the kit's `closeOnClick` radios did. */
function DeliverableRow({
  choice,
  selected,
  onPick,
  label,
  hint,
  showIcon = true,
}: {
  choice: DeliverableChoice;
  selected: boolean;
  onPick: (choice: DeliverableChoice) => void;
  label?: string;
  hint?: string;
  /** Root rows wear their kind's glyph; the format flyout stays quieter without one. */
  showIcon?: boolean;
}) {
  const row = DELIVERABLES.find((d) => d.value === choice);
  return (
    <MenuItem
      selected={selected}
      icon={showIcon && row ? <Icon icon={row.icon} /> : undefined}
      label={label ?? row?.label ?? choice}
      description={hint ?? DELIVERABLE_HINTS[choice]}
      onClick={() => onPick(choice)}
    />
  );
}

/** The Deliverable dial (the path law) - ONE menu for the kind AND
 *  the document's format, with the format family NESTED under Document (
 *  the flat "Document · X" run read as five sibling kinds). Clicking
 *  **Document** selects it and closes - your words pick the format - while the row's
 *  chevron (hover or click) flies out the four formats; the flyout repeats a
 *  "Words decide" row so touch and keyboard users keep both doors. Deliberately BREAKS
 *  the row's word law: the one dial that decides WHAT GETS
 *  MADE always wears its word - "Auto" at rest - because the icon alone was
 *  undiscoverable. Fleet HIDES the dial at the mount site (the server 422s the pair -
 *  capability law: absent, never disabled-with-excuse). */
export function DeliverableDial({
  value,
  onChange,
}: {
  value: DeliverableChoice;
  onChange: (value: DeliverableChoice) => void;
}) {
  const row = (choice: DeliverableChoice) => (
    <DeliverableRow choice={choice} selected={value === choice} onPick={onChange} />
  );
  return (
    <Menu modal={false}>
      <MenuTrigger
        aria-label="Deliverable"
        title="Deliverable: what gets made, and for documents, which file"
        // The bar's recipe (connectors-control): ghost at rest, tertiary's fill once a kind is set.
        render={
          <ButtonElement
            variant={value === "auto" ? "ghost" : "tertiary"}
            size="md"
            stretch="auto"
          />
        }
      >
        <Icon icon={Shapes} />
        <span>{DELIVERABLES.find((d) => d.value === value)?.label}</span>
        <Icon icon={ChevronsUpDown} tint="tertiary" />
      </MenuTrigger>
      <MenuContent align="start">
        {row("auto")}
        <div className="flex items-stretch gap-0.5">
          <div className="min-w-0 flex-1">
            {/* The family row stays checked while a nested format is the real selection -
                without it, reopening on "Document · PDF" showed no checked row at root. */}
            <DeliverableRow
              choice="document"
              selected={value === "document" || value.startsWith("document:")}
              onPick={onChange}
            />
          </div>
          <MenuSubmenu>
            <div className="shrink-0">
              {/* Chevron only: the row beside it carries the word. */}
              <MenuSubmenuTrigger label="" aria-label="Document file formats" />
            </div>
            <MenuSubmenuContent>
              <DeliverableRow
                choice="document"
                selected={value === "document"}
                onPick={onChange}
                label="Words decide"
                hint=".md unless your ask names a format"
                showIcon={false}
              />
              <MenuSeparator />
              {DOCUMENT_FORMAT_CHOICES.map((choice) => (
                <DeliverableRow
                  key={choice}
                  choice={choice}
                  selected={value === choice}
                  onPick={onChange}
                  label={FORMAT_LABEL[choice.slice("document:".length) as DocumentFormat]}
                  showIcon={false}
                />
              ))}
            </MenuSubmenuContent>
          </MenuSubmenu>
        </div>
        {row("collection")}
        {row("spreadsheet")}
        {row("deck")}
        {row("code")}
      </MenuContent>
    </Menu>
  );
}

/** Flip one seat of the fleet; the last seat stays (a fleet needs a model) and says why. */
export function toggleFleetRef(refs: string[], ref: string, onChange: (refs: string[]) => void) {
  if (!refs.includes(ref)) return onChange([...refs, ref]);
  if (refs.length === 1) {
    toast.error("A fleet needs at least one model.");
    return;
  }
  onChange(refs.filter((r) => r !== ref));
}

/** The fleet picker - scales where a chips row cannot: the pill states the count, the panel
 *  scrolls; 3 models or 30, the bar stays one line. Controlled: the host owns `refs`. */
export function FleetModelsDial({
  models,
  refs,
  onChange,
}: {
  models: ModelOut[];
  refs: string[];
  onChange: (refs: string[]) => void;
}) {
  // Grouped for the picker: providers as section labels keep a 20-model catalog scannable.
  const byProvider = useMemo(() => {
    const groups = new Map<string, ModelOut[]>();
    for (const m of models) {
      const list = groups.get(m.provider) ?? [];
      list.push(m);
      groups.set(m.provider, list);
    }
    return [...groups.entries()];
  }, [models]);

  const toggle = (ref: string) => toggleFleetRef(refs, ref, onChange);

  return (
    <Popover>
      <PopoverTrigger
        aria-label="Fleet models"
        title="Fleet models: which seats answer"
        render={
          <Button
            // all seats is the default; a narrowed fleet wears the set fill
            variant={refs.length < models.length ? "tertiary" : "ghost"}
            size="md"
            stretch="auto"
            icon={<Icon icon={Boxes} />}
            label={`${refs.length}/${models.length}`}
          />
        }
      />
      <PopoverContent variant="rich" align="start">
        {/* w-72: the kit's 320px panel less Propel's padding; the rich panel is otherwise
            as wide as its longest line. */}
        <div className="w-72 space-y-3">
          <div className="space-y-1">
            <div className="flex items-baseline justify-between">
              <p className="text-body-sm-regular font-medium">Fleet models</p>
              {refs.length < models.length && (
                <button
                  type="button"
                  onClick={() => onChange(models.map((m) => m.ref))}
                  className="text-muted-foreground hover:text-foreground text-caption-md-regular transition-colors"
                >
                  Select all
                </button>
              )}
            </div>
            <p className="text-muted-foreground text-caption-md-regular">
              Every selected model answers in parallel; the chair compares the results. More models,
              more spend.
            </p>
          </div>
          <div className="scrollbar-thin -mx-1 max-h-64 space-y-2.5 overflow-y-auto px-1">
            {byProvider.map(([provider, group]) => (
              <div key={provider} className="space-y-0.5">
                <p className="text-muted-foreground px-2 text-caption-md-regular font-medium tracking-wide uppercase">
                  {provider}
                </p>
                {group.map((m) => {
                  const on = refs.includes(m.ref);
                  return (
                    <button
                      key={m.ref}
                      type="button"
                      aria-pressed={on}
                      title={m.ref}
                      onClick={() => toggle(m.ref)}
                      className="hover:bg-accent/40 flex min-h-7 w-full items-center gap-2 rounded-md px-2 py-1 text-left text-caption-md-regular/relaxed transition-colors"
                    >
                      <Check
                        className={cn("size-3.5 shrink-0", on ? "opacity-100" : "opacity-0")}
                      />
                      <span className="min-w-0 flex-1 truncate">{m.model_id}</span>
                    </button>
                  );
                })}
              </div>
            ))}
          </div>
        </div>
      </PopoverContent>
    </Popover>
  );
}
