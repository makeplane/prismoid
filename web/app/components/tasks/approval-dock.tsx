/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The approval dock (ADR-054) - the QuestionDock's sibling for `approve` gates: a step
// paused on side-effectful connector calls, waiting for Allow or Deny. Same floating
// grammar, different verbs: the title is the headline, the evidence renders as one block
// per call (label line + capped args preview, mono), and ONE Allow/Deny pair resolves the
// whole batch - the wire resolves per gate, not per call. Deny is a decline (the run
// continues without the action), never a cancel; Cancel task stays the inbox card's verb.

import { Button } from "@makeplane/propel/components/button";
import { Icon } from "@makeplane/propel/components/icon";
import { IconButton } from "@makeplane/propel/components/icon-button";
import {
  RadioGroupField,
  RadioGroupFieldOption,
} from "@makeplane/propel/components/radio-group-field";
import { ShieldCheck, X } from "lucide-react";
import { useState } from "react";
import { toast } from "@prismoid/blocks/toast";
import { api, toastError, unwrap } from "~/lib/api/client";
import { personName } from "~/lib/format";
import type { GateView } from "~/lib/task-view";
import { cn } from "~/lib/utils";

/** The dock/card shell recipe - the floating grammar QuestionDock wears; shared as a
 *  constant so the two docks cannot drift chrome. */
export const DOCK_SHELL =
  "bg-popover ring-foreground/10 rounded-xl p-2 shadow-(--shadow-overlay) ring-1";

export type ApprovalItem = { label: string; preview: string };

/** The gate's `reason` is one block per call: a "Connector: tool" label line, then the
 *  capped args preview, blocks blank-line separated (operations/gates.open_approval writes
 *  exactly this shape; a no-args call is just the label line). An empty reason renders one
 *  block wearing the gate's title - the dock never hides a gate it cannot parse. */
export function parseApprovalItems(gate: Pick<GateView, "title" | "reason">): ApprovalItem[] {
  const raw = (gate.reason ?? "").trim();
  if (!raw) return [{ label: gate.title ?? "Approve this action?", preview: "" }];
  return raw.split(/\n\n+/).map((block) => {
    const [first, ...rest] = block.split("\n");
    return { label: first, preview: rest.join("\n") };
  });
}

export function approvalHeading(gate: Pick<GateView, "title" | "reason">): string {
  return gate.title ?? "Waiting on your approval";
}

/** Why the approval rules asked, as the card says it first ("Asks because it moves money."); null
 *  for a gate that carries no rule (older gates, or a browser ask whose evidence says why). Pure. */
export function approvalReason(gate: Pick<GateView, "asks_because">): string | null {
  const why = (gate.asks_because ?? []).map((w) => w.trim()).filter(Boolean);
  return why.length ? `Asks because ${why.join("; ")}.` : null;
}

/** The reason line every approve face draws above its evidence (`approvalReason`); nothing without one. */
export function ApprovalReason({ gate, className }: { gate: GateView; className?: string }) {
  const reason = approvalReason(gate);
  return reason ? <p className={cn("text-body-sm-regular", className)}>{reason}</p> : null;
}

// The scope choice - shared by every approve-face surface
// (this dock, GateCard) so they cannot drift on order, labels, or the accept body's shape.

const SCOPE_LABELS: [string, (host?: string) => string][] = [
  ["once", () => "Just this once"],
  ["task", () => "For this task"],
  ["always", (host) => `Always allow ${host}`],
];

/** The three scopes, in their fixed order, filtered to what THIS gate actually offers - a
 *  danger ask (`scopes: []`, or a connector gate with no `scopes` at all) resolves to no
 *  options, which is how a surface decides whether to render the radio group at all. Pure,
 *  so it is tested without a DOM. */
export function scopeOptions(
  gate: Pick<GateView, "scopes" | "host">,
): { value: string; label: string }[] {
  const offered = gate.scopes ?? [];
  return SCOPE_LABELS.filter(([value]) => offered.includes(value)).map(([value, label]) => ({
    value,
    label: label(gate.host),
  }));
}

/** The radio's initial pick: "task" when the gate offers it (the middle ground between a
 *  one-off click and a standing allowance), else whatever the gate DOES offer first - a
 *  gate is never rendered with no selection. */
export function defaultScope(options: { value: string; label: string }[]): string {
  return options.find((o) => o.value === "task")?.value ?? options[0]?.value ?? "task";
}

/** Who may say yes to THIS gate (the server's `gates.resolve` rule, so the dock never
 *  offers a verb it would refuse): a connector approval runs on the task creator's own
 *  connection, so only the creator may Allow it; a browser approval is any editor's to allow
 *  once or for the task, but "always" writes a standing allowance on the creator's account and
 *  is theirs alone. Anyone who can see the dock may Deny (saying no spends nothing). Pure. */
export function approvalAccess(
  gate: Pick<GateView, "source" | "scopes" | "host">,
  isCreator: boolean,
): { allow: boolean; options: { value: string; label: string }[] } {
  const offered = scopeOptions(gate);
  if (isCreator) return { allow: true, options: offered };
  if (gate.source !== "browser") return { allow: false, options: [] };
  return { allow: true, options: offered.filter((o) => o.value !== "always") };
}

/** The approve accept's `content`: the chosen scope when the gate offered any, `undefined`
 *  for a danger ask (`scopes: []`) - which keeps the plain `{action: "accept"}` wire, no
 *  question fields to build. Pure, exported so a click handler's body needs no DOM. */
export function approveScopeContent(
  gate: Pick<GateView, "scopes">,
  scope: string,
): Record<string, unknown> | undefined {
  return gate.scopes?.length ? { scope } : undefined;
}

/** A resolve's answer read for a steer: a steer that landed while this approval
 *  waited closed it as a DECLINE naming the steer (`superseded_by`), and whoever clicks after
 *  is handed that resolution (HTTP 200). It reads as superseded, by whose steer - never
 *  "approved", never "answered first". Null for every other answer. Shared with GateCard. */
export function supersededNote(res: {
  action?: string;
  content?: Record<string, unknown> | null;
  resolved_by?: string | null;
}): string | null {
  if (res.action !== "decline" || typeof res.content?.superseded_by !== "string") return null;
  return res.resolved_by?.trim()
    ? `Superseded by ${personName(res.resolved_by)}'s steer`
    : "Superseded by a steer";
}

export function ApprovalDock({
  gate,
  onResolved,
  className,
  isCreator = true,
  creatorName,
}: {
  gate: GateView;
  /** Fired once the resolve lands, with WHO the server says resolved it (see QuestionDock). */
  onResolved?: (resolvedBy?: string, resolvedById?: string | null) => void;
  className?: string;
  /** Is the viewer the task's creator (`TaskDetailOut.is_creator`)? Decides `approvalAccess`. */
  isCreator?: boolean;
  /** Who the others are waiting on, when the viewer may not allow it. */
  creatorName?: string | null;
}) {
  const [busy, setBusy] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  // A late Allow lost to a steer: the card says so until the gate's frame clears it.
  const [superseded, setSuperseded] = useState<string | null>(null);
  const items = parseApprovalItems(gate);
  const { allow, options } = approvalAccess(gate, isCreator);
  const [scope, setScope] = useState(() => defaultScope(options));

  async function resolve(action: "accept" | "decline") {
    if (busy) return;
    setBusy(true);
    try {
      const content = action === "accept" ? approveScopeContent(gate, scope) : undefined;
      const res = await unwrap(
        api.POST("/v1/gates/{gate_id}/resolve", {
          params: { path: { gate_id: gate.gate_id } },
          body: { action, ...(content ? { content } : {}) },
        }),
      );
      const note = supersededNote(res);
      if (note) {
        setSuperseded(note);
        toast(note, { description: "The action did not run." });
        return;
      }
      onResolved?.(res.resolved_by, res.resolved_by_id);
    } catch (e) {
      toastError(e, action === "accept" ? "Couldn't allow it." : "Couldn't deny it.");
    } finally {
      // Always release the latch - the stacked-gates key-remount law (QuestionDock's).
      setBusy(false);
    }
  }

  if (collapsed) {
    return (
      <button
        type="button"
        onClick={() => setCollapsed(false)}
        className={cn(
          "bg-popover ring-foreground/10 shadow-(--shadow-overlay) hover:bg-muted/40 flex w-full items-center justify-between rounded-xl px-4 py-2.5 text-left ring-1 transition-colors",
          className,
        )}
      >
        <span className="text-body-sm-regular font-medium">
          {items.length === 1 ? "1 approval waiting" : `${items.length} approvals waiting`}
        </span>
        <span className="text-muted-foreground text-caption-md-regular">Review</span>
      </button>
    );
  }

  return (
    <div className={cn(DOCK_SHELL, className)}>
      <div className="flex items-start gap-2 px-2 pt-1.5 pb-2">
        <span className="bg-muted text-muted-foreground mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-md">
          <ShieldCheck className="size-3.5" />
        </span>
        {/* the headline, WHOLE - a permission you can't read is one you can't grant */}
        <h3 className="min-w-0 flex-1 pt-1 text-body-sm-regular font-medium break-words">
          {approvalHeading(gate)}
        </h3>
        <IconButton
          variant="ghost"
          size="sm"
          aria-label="Hide the approval"
          onClick={() => setCollapsed(true)}
          icon={<Icon icon={X} />}
        />
      </div>

      <div className="space-y-1.5 px-2 pb-2">
        <ApprovalReason gate={gate} className="px-1" />
        {items.map((item, i) => (
          <div key={`${item.label}-${i}`}>
            {items.length > 1 && (
              <p className="text-muted-foreground pb-0.5 font-mono text-caption-md-regular">
                {item.label}
              </p>
            )}
            {item.preview && (
              <pre className="bg-muted/40 max-h-40 overflow-y-auto rounded-lg p-2 font-mono text-caption-md-regular whitespace-pre-wrap">
                {item.preview}
              </pre>
            )}
          </div>
        ))}
      </div>

      {options.length > 0 && (
        <div className="px-2 pb-2">
          <RadioGroupField
            label="Allow for"
            size="md"
            density="compact"
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

      {superseded ? (
        <p className="text-muted-foreground px-3 pb-1.5 text-caption-md-regular" role="status">
          {superseded} - the action did not run.
        </p>
      ) : (
        <div className="flex items-center justify-end gap-2 px-2 pb-1">
          <span className="text-muted-foreground mr-auto pl-1 text-caption-md-regular">
            {allow
              ? "Denying skips the action; the task continues."
              : `Waiting on ${creatorName?.trim() || "the task's creator"}. It runs on their connection.`}
          </span>
          <Button
            variant="secondary"
            size="sm"
            stretch="auto"
            disabled={busy}
            onClick={() => void resolve("decline")}
            label="Deny"
          />
          {allow && (
            <Button
              variant="primary"
              size="sm"
              stretch="auto"
              disabled={busy}
              onClick={() => void resolve("accept")}
              label="Allow"
            />
          )}
        </div>
      )}
    </div>
  );
}
