/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useEffect, useState } from "react";
import { toast } from "@prismoid/blocks/toast";
import { Button } from "@makeplane/propel/components/button";
import { CheckboxGroupFieldOption } from "@makeplane/propel/components/checkbox-group-field";
import { Field, FieldError, FieldLabel } from "@makeplane/propel/components/field";
import { Input, InputGroup } from "@makeplane/propel/components/input";
import { RadioGroup } from "@makeplane/propel/components/radio";
import {
  RadioGroupField,
  RadioGroupFieldOption,
} from "@makeplane/propel/components/radio-group-field";
import { TextArea, TextAreaGroup } from "@makeplane/propel/components/text-area";
import { TONE_CARD } from "~/components/kit";
import {
  ApprovalReason,
  approveScopeContent,
  defaultScope,
  parseApprovalItems,
  scopeOptions,
  supersededNote,
} from "~/components/tasks/approval-dock";
import { BudgetDock, budgetFace } from "~/components/tasks/budget-dock";
import { ConnectGateBody } from "~/components/tasks/connect-dock";
import { toastError, api, unwrap } from "~/lib/api/client";
import type { Question } from "~/lib/api/events";
import { formatSeconds } from "~/lib/time";
import { cn } from "~/lib/utils";
import { dockFor, type GateView } from "~/lib/task-view";

const OTHER = "__other__";

function secondsLeft(expiresAt?: string): number | null {
  if (!expiresAt) return null;
  const ms = Date.parse(expiresAt) - Date.now();
  return Number.isNaN(ms) ? null : Math.max(0, Math.floor(ms / 1000));
}

/** A budget gate with no task in hand: the reason and its honest verbs. A "Spending allowance
 *  reached" gate offers Cancel task alone, as the dock does: the allowance coming back resumes
 *  it, never a person's click. */
function BudgetCard({
  gate,
  busy,
  resolve,
}: {
  gate: GateView;
  busy: boolean;
  resolve: (action: "accept" | "cancel") => void;
}) {
  const allowanceSpent = budgetFace(gate.budget_scope) === "workspace";
  return (
    <div className={cn(TONE_CARD.warning, "rounded-xl border p-5")}>
      <h3 className="font-medium">{gate.title ?? "Budget reached"}</h3>
      {gate.reason && (
        <p className="text-muted-foreground mt-1 text-body-sm-regular">{gate.reason}</p>
      )}
      <div className="mt-5 flex flex-wrap items-center gap-2">
        {!allowanceSpent && (
          <Button
            variant="primary"
            size="md"
            stretch="auto"
            label="Resume"
            onClick={() => resolve("accept")}
            disabled={busy}
          />
        )}
        <div className="ml-auto">
          <Button
            variant="ghost"
            size="md"
            stretch="auto"
            label="Cancel task"
            onClick={() => resolve("cancel")}
            disabled={busy}
          />
        </div>
      </div>
    </div>
  );
}

/**
 * The gate form. Renders a question set numbered 1..N across all five types, honours `allow_other` and
 * `required`, counts down to expiry, and resolves via the three MCP-aligned actions
 * (accept | decline | cancel). On success the `gate.resolved` frame removes this card from the stream;
 * replay-fed surfaces (the inbox answers gates in place) refetch via `onResolved` instead.
 * Promoted from routes/tasks.$id on its second consumer.
 */
export function GateCard({
  gate,
  onResolved,
  budgetTask,
  isCreator = false,
}: {
  gate: GateView;
  onResolved?: () => void;
  /** The task's creator: on a device, the only one who says Done. */
  isCreator?: boolean;
  /** What a "Budget reached" face needs from the task (id, Project, spend) to raise the ceiling in
   *  place. Without it the card still never offers a decline: just Resume and Cancel task. */
  budgetTask?: { id: string; projectId: string | null; spentUsd: number };
}) {
  const [values, setValues] = useState<Record<string, string | string[]>>({});
  const [other, setOther] = useState<Record<string, string>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<null | "accept" | "decline" | "cancel">(null);
  // An approve gate's scope choice - "task" when offered
  // (the middle ground between a one-off click and a standing allowance), else whatever
  // the gate DOES offer first.
  const [scope, setScope] = useState(() => defaultScope(scopeOptions(gate)));
  const [left, setLeft] = useState<number | null>(() => secondsLeft(gate.expires_at));

  useEffect(() => {
    if (gate.expires_at == null) return;
    const id = window.setInterval(() => setLeft(secondsLeft(gate.expires_at)), 1000);
    return () => window.clearInterval(id);
  }, [gate.expires_at]);

  function setValue(q: Question, v: string | string[]) {
    setValues((cur) => ({ ...cur, [q.id]: v }));
    setErrors((cur) => ({ ...cur, [q.id]: "" }));
  }

  function coerce(q: Question): unknown {
    const raw = values[q.id];
    switch (q.type) {
      case "text":
        return typeof raw === "string" ? raw.trim() : "";
      case "number":
        return raw === undefined || raw === "" ? undefined : Number(raw);
      case "boolean":
        return raw === "true";
      case "single_select":
        return raw === OTHER ? (other[q.id]?.trim() ?? "") : (raw ?? "");
      case "multi_select": {
        const arr = Array.isArray(raw) ? raw : [];
        return arr.map((x) => (x === OTHER ? (other[q.id]?.trim() ?? "") : x)).filter(Boolean);
      }
    }
  }

  /** What the person filled in, and each required question they left empty. */
  function answered(): { given: Record<string, unknown>; missing: Record<string, string> } {
    const given: Record<string, unknown> = {};
    const missing: Record<string, string> = {};
    for (const q of gate.questions) {
      const value = coerce(q);
      const empty =
        value === undefined || value === "" || (Array.isArray(value) && value.length === 0);
      if (q.required && empty) missing[q.id] = "This one's required.";
      if (!empty) given[q.id] = value;
    }
    return { given, missing };
  }

  async function resolve(action: "accept" | "decline" | "cancel") {
    let content: Record<string, unknown> | undefined;
    if (action === "accept" && gate.kind === "approve") {
      // No question fields on an approve gate - the scope IS the content, and only when the
      // gate offered any (a danger ask offers none and keeps the plain accept/deny wire).
      content = approveScopeContent(gate, scope);
    } else if (action === "accept") {
      const { given, missing } = answered();
      if (Object.keys(missing).length) {
        setErrors(missing);
        return;
      }
      content = given;
    } else if (action === "decline") {
      // "Proceed anyway" takes the defaults for the rest: what was filled in stands.
      const { given } = answered();
      if (Object.keys(given).length) content = given;
    }
    setBusy(action);
    try {
      const res = await unwrap(
        api.POST("/v1/gates/{gate_id}/resolve", {
          params: { path: { gate_id: gate.gate_id } },
          body: { action, ...(content ? { content } : {}) },
        }),
      );
      // A steer closed this approval first: say so - the card is about to go.
      const note = supersededNote(res);
      if (note) toast(note, { description: "The action did not run." });
      onResolved?.();
    } catch (e) {
      setBusy(null);
      toastError(e, "Couldn't submit your answer.");
    }
  }

  const expired = left === 0;

  // The budget face: a "Budget reached" gate has no questions, and a decline
  // CANCELS the task, so the generic face's "Decline · proceed anyway" would kill it. With the task
  // in hand it is the same dock the task page mounts; without, the reason and two honest verbs.
  if (dockFor(gate) === "budget") {
    if (budgetTask) {
      return (
        <BudgetDock
          gate={gate}
          taskId={budgetTask.id}
          projectId={budgetTask.projectId}
          spentUsd={budgetTask.spentUsd}
          onResolved={() => onResolved?.()}
        />
      );
    }
    return (
      <BudgetCard gate={gate} busy={busy !== null} resolve={(action) => void resolve(action)} />
    );
  }

  // The connect face: the same in-conversation block the task page docks -
  // brand card, one-click connect, self-accepting on live - plus the card's escape hatch.
  if (gate.kind === "connect") {
    return (
      <div className={cn(TONE_CARD.warning, "rounded-xl border p-5")}>
        <ConnectGateBody
          gate={gate}
          onResolved={onResolved}
          footer={
            <Button
              variant="ghost"
              size="sm"
              stretch="auto"
              label="Cancel task"
              onClick={() => void resolve("cancel")}
              disabled={busy !== null}
            />
          }
        />
      </div>
    );
  }

  // The approve face: no question fields - the evidence blocks and three verbs.
  // Allow = accept, Deny = decline (the run continues without the action), Cancel task
  // stays the card's escape hatch, per the three-verbs law.
  if (gate.kind === "approve") {
    const options = scopeOptions(gate);
    return (
      <div className={cn(TONE_CARD.warning, "rounded-xl border p-5")}>
        <div className="mb-4 flex items-start justify-between gap-3">
          <div>
            <h3 className="font-medium">{gate.title ?? "Waiting on your approval"}</h3>
            <p className="text-muted-foreground text-body-sm-regular">
              Allow to run the action; deny to skip it and let the task continue without.
            </p>
          </div>
          {left != null && (
            <span
              className={cn(
                "text-caption-md-regular tabular-nums",
                expired ? "text-destructive" : "text-muted-foreground",
              )}
            >
              {expired ? "expired" : `expires in ${formatSeconds(left)}`}
            </span>
          )}
        </div>
        <div className="space-y-1.5">
          <ApprovalReason gate={gate} />
          {parseApprovalItems(gate).map((item, i) => (
            <div key={`${item.label}-${i}`}>
              <p className="text-muted-foreground pb-0.5 font-mono text-caption-md-regular">
                {item.label}
              </p>
              {item.preview && (
                <pre className="bg-muted/40 max-h-40 overflow-y-auto rounded-lg p-2 font-mono text-caption-md-regular whitespace-pre-wrap">
                  {item.preview}
                </pre>
              )}
            </div>
          ))}
        </div>
        {options.length > 0 && (
          <div className="mt-4">
            <RadioGroupField
              size="lg"
              density="compact"
              label="Allow for"
              name={`scope-${gate.gate_id}`}
              value={scope}
              onValueChange={(v) => setScope(String(v ?? ""))}
            >
              {options.map((o) => (
                <RadioGroupFieldOption key={o.value} value={o.value} label={o.label} />
              ))}
            </RadioGroupField>
          </div>
        )}
        <div className="mt-5 flex flex-wrap items-center gap-2">
          <Button
            variant="primary"
            size="md"
            stretch="auto"
            label="Allow"
            onClick={() => void resolve("accept")}
            disabled={busy !== null || expired}
          />
          <Button
            variant="secondary"
            size="md"
            stretch="auto"
            label="Deny · continue without"
            onClick={() => void resolve("decline")}
            disabled={busy !== null}
          />
          <div className="ml-auto">
            <Button
              variant="ghost"
              size="md"
              stretch="auto"
              label="Cancel task"
              onClick={() => void resolve("cancel")}
              disabled={busy !== null}
            />
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className={cn(TONE_CARD.warning, "rounded-xl border p-5")}>
      <div className="mb-4 flex items-start justify-between gap-3">
        <div>
          <h3 className="font-medium">{gate.title ?? "A few questions before continuing"}</h3>
          <p className="text-muted-foreground text-body-sm-regular">
            Answer to keep going, or decline to let Prismoid proceed on its own.
          </p>
        </div>
        {left != null && (
          <span
            className={cn(
              "text-caption-md-regular tabular-nums",
              expired ? "text-destructive" : "text-muted-foreground",
            )}
          >
            {expired ? "expired" : `expires in ${formatSeconds(left)}`}
          </span>
        )}
      </div>

      <ol className="space-y-5">
        {gate.questions.map((q, i) => (
          <li key={q.id}>
            <Field size="lg" invalid={errors[q.id] ? true : undefined}>
              <FieldLabel size="lg" inset={false} required={q.required}>
                <span className="flex gap-2">
                  <span className="text-muted-foreground">{i + 1}.</span>
                  <span>{q.prompt}</span>
                </span>
              </FieldLabel>
              <QuestionField
                q={q}
                value={values[q.id]}
                other={other[q.id] ?? ""}
                onValue={(v) => setValue(q, v)}
                onOther={(v) => setOther((cur) => ({ ...cur, [q.id]: v }))}
              />
              {errors[q.id] && (
                <FieldError size="lg" match>
                  {errors[q.id]}
                </FieldError>
              )}
            </Field>
          </li>
        ))}
      </ol>

      <QuestionActions
        gate={gate}
        busy={busy}
        expired={expired}
        isCreator={isCreator}
        resolve={(action) => void resolve(action)}
      />
    </div>
  );
}

/** The question card's buttons: submit (or the creator's Done on a device), decline, cancel. */
function QuestionActions({
  gate,
  busy,
  expired,
  isCreator,
  resolve,
}: {
  gate: GateView;
  busy: string | null;
  expired: boolean;
  isCreator: boolean;
  resolve: (action: "accept" | "decline" | "cancel") => void;
}) {
  return (
    <div className="mt-5 flex flex-wrap items-center gap-2">
      {/* a CONTROL gate has nothing to submit (a hand-back answers it); a budget or cost
            confirmation gate still needs the accept */}
      {dockFor(gate) !== "control" && (
        <Button
          variant="primary"
          size="md"
          stretch="auto"
          label="Submit answers"
          onClick={() => resolve("accept")}
          disabled={busy !== null || expired}
        />
      )}
      {/* ...except on a device: the browser is a window on the
            creator's own computer, and their Done (an accept) hands it back - theirs only. */}
      {dockFor(gate) === "control" && gate.machine_driver === "device" && isCreator && (
        <Button
          variant="primary"
          size="md"
          stretch="auto"
          label="Done"
          onClick={() => resolve("accept")}
          disabled={busy !== null || expired}
        />
      )}
      <Button
        variant="secondary"
        size="md"
        stretch="auto"
        label="Decline · proceed anyway"
        onClick={() => resolve("decline")}
        disabled={busy !== null}
      />
      <div className="ml-auto">
        <Button
          variant="ghost"
          size="md"
          stretch="auto"
          label="Cancel task"
          onClick={() => resolve("cancel")}
          disabled={busy !== null}
        />
      </div>
    </div>
  );
}

function QuestionField({
  q,
  value,
  other,
  onValue,
  onOther,
}: {
  q: Question;
  value: string | string[] | undefined;
  other: string;
  onValue: (v: string | string[]) => void;
  onOther: (v: string) => void;
}) {
  const options = q.options ?? [];

  if (q.type === "text") {
    return (
      <TextAreaGroup resize="none">
        <TextArea
          size="lg"
          surface="field"
          autoResize
          rows={2}
          value={typeof value === "string" ? value : ""}
          onChange={(e) => onValue(e.currentTarget.value)}
          placeholder="Your answer…"
        />
      </TextAreaGroup>
    );
  }
  if (q.type === "number") {
    return (
      <InputGroup size="lg">
        <Input
          size="lg"
          inputMode="decimal"
          value={typeof value === "string" ? value : ""}
          onChange={(e) => onValue(e.currentTarget.value)}
          placeholder="A number…"
        />
      </InputGroup>
    );
  }
  if (q.type === "boolean") {
    return (
      <RadioGroup
        density="compact"
        value={typeof value === "string" ? value : ""}
        onValueChange={(v) => onValue(String(v ?? ""))}
      >
        {[
          { v: "true", label: "Yes" },
          { v: "false", label: "No" },
        ].map((o) => (
          <RadioGroupFieldOption key={o.v} size="lg" value={o.v} label={o.label} />
        ))}
      </RadioGroup>
    );
  }
  if (q.type === "single_select") {
    const selected = typeof value === "string" ? value : "";
    return (
      <RadioGroup
        density="compact"
        value={selected}
        onValueChange={(v) => onValue(String(v ?? ""))}
      >
        {options.map((o) => (
          <RadioGroupFieldOption key={o.value} size="lg" value={o.value} label={o.label} />
        ))}
        {q.allow_other && (
          <div className="space-y-1.5">
            <RadioGroupFieldOption size="lg" value={OTHER} label="Other" />
            {selected === OTHER && (
              <InputGroup size="lg">
                <Input
                  size="lg"
                  value={other}
                  onChange={(e) => onOther(e.currentTarget.value)}
                  placeholder="Please specify…"
                />
              </InputGroup>
            )}
          </div>
        )}
      </RadioGroup>
    );
  }
  // multi_select
  const selected = Array.isArray(value) ? value : [];
  const toggle = (v: string, on: boolean) =>
    onValue(on ? [...selected, v] : selected.filter((x) => x !== v));
  return (
    // Each option row is its own Field.Item scope, so a box is named by its option, not the prompt.
    <div>
      {options.map((o) => (
        <CheckboxGroupFieldOption
          key={o.value}
          size="lg"
          label={o.label}
          checked={selected.includes(o.value)}
          onCheckedChange={(c) => toggle(o.value, c === true)}
        />
      ))}
      {q.allow_other && (
        <div className="space-y-1.5">
          <CheckboxGroupFieldOption
            size="lg"
            label="Other"
            checked={selected.includes(OTHER)}
            onCheckedChange={(c) => toggle(OTHER, c === true)}
          />
          {selected.includes(OTHER) && (
            <InputGroup size="lg">
              <Input
                size="lg"
                value={other}
                onChange={(e) => onOther(e.currentTarget.value)}
                placeholder="Please specify…"
              />
            </InputGroup>
          )}
        </div>
      )}
    </div>
  );
}
