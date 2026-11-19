/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The dedicated creation surface: "+ New routine" opens HERE, on the
// routines screen - the /projects CreateProject grammar, not a hand-off to the home composer (the
// old `/?when=1` jump read as the app teleporting away; creation belongs to the resource's own
// page). The composer's When dial still exists for the organic path - "save this send as
// standing" - but this dialog is the front door. The no-builder law holds: the When is presets,
// a cron, an interval, or event kinds, and the copy always reads it back as one sentence.

import { Button } from "@makeplane/propel/components/button";
import {
  Dialog,
  DialogActions,
  DialogBody,
  DialogClose,
  DialogCloseGroup,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogHeading,
  DialogInfo,
  DialogMain,
  DialogTitle,
  DialogTrigger,
} from "@makeplane/propel/components/dialog";
import { Icon } from "@makeplane/propel/components/icon";
import { IconButton } from "@makeplane/propel/components/icon-button";
import { Input, InputGroup } from "@makeplane/propel/components/input";
import { InputField } from "@makeplane/propel/components/input-field";
import { TextAreaField } from "@makeplane/propel/components/text-area-field";
import { useState } from "react";
import { Plus, X } from "lucide-react";
import { toast } from "@prismoid/blocks/toast";
import { api, toastError, unwrap } from "~/lib/api/client";
import { formatDateTime } from "~/lib/format";
import { cn } from "~/lib/utils";
import {
  EVENT_KINDS,
  PRESETS,
  whenLabel,
  type WhenValue,
} from "~/components/composer/when-control";

/** The dialog's When: a routine must carry one, so "now" is not on the menu here. */
type RoutineWhen = Exclude<WhenValue, { mode: "now" }>;

export function NewRoutine({ onCreated }: { onCreated: (id: string) => void }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [instruction, setInstruction] = useState("");
  const [when, setWhen] = useState<RoutineWhen | null>(null);
  const [cronDraft, setCronDraft] = useState("");
  const [everyDraft, setEveryDraft] = useState("");
  const [budget, setBudget] = useState("");
  const [busy, setBusy] = useState(false);

  const reset = () => {
    setName("");
    setInstruction("");
    setWhen(null);
    setCronDraft("");
    setEveryDraft("");
    setBudget("");
  };

  // Picking from one lane clears the other - the create wire's schedule-XOR-events, visible.
  const pickSchedule = (w: RoutineWhen) => setWhen(w);
  const toggleKind = (kind: string) => {
    const kinds = when?.mode === "event" ? when.kinds : [];
    const next = kinds.includes(kind) ? kinds.filter((k) => k !== kind) : [...kinds, kind];
    setWhen(next.length ? { mode: "event", kinds: next } : null);
    setCronDraft("");
    setEveryDraft("");
  };

  const ready = name.trim().length > 0 && instruction.trim().length > 0 && when !== null;

  async function create() {
    if (!ready || when === null) return;
    setBusy(true);
    try {
      const budgetNum = Number(budget.trim());
      const saved = await unwrap(
        api.POST("/v1/routines", {
          body: {
            name: name.trim(),
            instruction: instruction.trim(),
            ...(when.mode === "schedule"
              ? { schedule: { cron: when.cron ?? null, every: when.every ?? null } }
              : { on_events: when.kinds }),
            ...(Number.isFinite(budgetNum) && budgetNum > 0 ? { budget_usd: budgetNum } : {}),
          },
        }),
      );
      toast.success(
        `Routine saved - ${whenLabel(when)}${saved.upcoming[0] ? `, first fire ${formatDateTime(saved.upcoming[0])}` : ""}.`,
      );
      setOpen(false);
      reset();
      onCreated(saved.id);
    } catch (e) {
      toastError(e, "Couldn't save the routine.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger
        render={
          <Button
            variant="primary"
            size="sm"
            stretch="auto"
            icon={<Icon icon={Plus} />}
            label="New routine"
          />
        }
      />
      <DialogContent size="xs">
        <DialogCloseGroup>
          <IconButton
            variant="ghost"
            size="xs"
            aria-label="Close"
            icon={<Icon icon={X} />}
            render={<DialogClose />}
          />
        </DialogCloseGroup>
        <DialogMain>
          <DialogHeader>
            <DialogHeading>
              <DialogTitle>New routine</DialogTitle>
              <DialogDescription>
                A task you save instead of send. Every run is an ordinary task - planned, budgeted,
                verified - and you can pause the routine any time.
              </DialogDescription>
            </DialogHeading>
          </DialogHeader>
          {/* The body scrolls under the viewport cap; the actions stay pinned below it. */}
          <DialogBody tabIndex={0}>
            <div className="flex flex-col gap-3">
              <InputField
                size="lg"
                orientation="vertical"
                label="Name"
                value={name}
                onChange={(e) => setName(e.currentTarget.value)}
                placeholder="monday-briefing"
                maxLength={120}
                autoFocus
              />

              <TextAreaField
                size="lg"
                autoResize
                label="Instruction"
                value={instruction}
                onChange={(e) => setInstruction(e.currentTarget.value)}
                placeholder="Each run starts fresh, with no memory of today. Write it self-contained."
                maxLength={8000}
              />

              <fieldset className="space-y-2">
                <legend className="text-muted-foreground mb-2 text-caption-md-regular font-medium uppercase">
                  On a schedule
                </legend>
                <div className="grid grid-cols-2 gap-1.5">
                  {PRESETS.map((p) => (
                    <button
                      key={p.cron}
                      type="button"
                      onClick={() => pickSchedule({ mode: "schedule", cron: p.cron })}
                      className={cn(
                        "hover:bg-accent/50 rounded-md border px-2.5 py-1.5 text-left text-caption-md-regular",
                        when?.mode === "schedule" && when.cron === p.cron && "border-primary",
                      )}
                    >
                      {p.label}
                    </button>
                  ))}
                </div>
                <div className="flex gap-1.5">
                  <div className="min-w-0 flex-1">
                    <InputGroup size="lg">
                      <Input
                        size="lg"
                        value={cronDraft}
                        onChange={(e) => {
                          setCronDraft(e.currentTarget.value);
                          setEveryDraft("");
                          const v = e.currentTarget.value.trim();
                          setWhen(v ? { mode: "schedule", cron: v } : null);
                        }}
                        placeholder='cron, e.g. "30 8 * * 1-5"'
                      />
                    </InputGroup>
                  </div>
                  <div className="w-28 shrink-0">
                    <InputGroup size="lg">
                      <Input
                        size="lg"
                        value={everyDraft}
                        onChange={(e) => {
                          setEveryDraft(e.currentTarget.value);
                          setCronDraft("");
                          const v = e.currentTarget.value.trim();
                          setWhen(v ? { mode: "schedule", every: v } : null);
                        }}
                        placeholder='every, e.g. "4h"'
                      />
                    </InputGroup>
                  </div>
                </div>
              </fieldset>

              <fieldset className="space-y-2">
                <legend className="text-muted-foreground mb-2 text-caption-md-regular font-medium uppercase">
                  Or when an event lands
                </legend>
                <div className="grid grid-cols-2 gap-1.5">
                  {EVENT_KINDS.map((e) => {
                    const on = when?.mode === "event" && when.kinds.includes(e.kind);
                    return (
                      <button
                        key={e.kind}
                        type="button"
                        onClick={() => toggleKind(e.kind)}
                        className={cn(
                          "hover:bg-accent/50 flex w-full items-center justify-between rounded-md border px-2.5 py-1.5 text-left text-caption-md-regular",
                          on && "border-primary",
                        )}
                      >
                        {e.label}
                        {on && <span aria-hidden>✓</span>}
                      </button>
                    );
                  })}
                </div>
                {when?.mode === "event" && (
                  <p className="text-muted-foreground text-caption-md-regular">
                    Event routines start held: each run waits for your release until you flip it.
                  </p>
                )}
              </fieldset>

              {/* Horizontal fields split label and control evenly; the width keeps the input short. */}
              <div className="w-64">
                <InputField
                  size="lg"
                  orientation="horizontal"
                  label="Budget per run"
                  startIcon={<span className="text-muted-foreground">$</span>}
                  value={budget}
                  onChange={(e) => setBudget(e.currentTarget.value)}
                  placeholder="2.00"
                  inputMode="decimal"
                />
              </div>
            </div>
          </DialogBody>
        </DialogMain>
        <DialogActions>
          <DialogInfo>
            <span className="truncate">
              {when ? whenLabel(when) : "Pick a When - a routine needs one."}
            </span>
          </DialogInfo>
          <Button
            variant="primary"
            size="md"
            stretch="auto"
            onClick={() => void create()}
            disabled={busy || !ready}
            label="Save routine"
          />
        </DialogActions>
      </DialogContent>
    </Dialog>
  );
}
