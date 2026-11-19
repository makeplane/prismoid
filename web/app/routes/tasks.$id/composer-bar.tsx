/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { type MutableRefObject, type ReactNode, type RefObject } from "react";
import type { Attachment } from "~/components/composer/capture";
import { Composer, type ComposerApi } from "~/components/composer/composer";
import { ConnectorsControl } from "~/components/composer/connectors-control";
import { DeliverableDial, FleetModelsDial, SizeDial } from "~/components/composer/dials";
import { ConnectorSuggestionChip } from "~/components/connectors/suggestion-chip";
import { GateDock } from "~/components/tasks/gate-dock";
import type { QuestionDockHandle } from "~/components/tasks/question-dock";
import type { components } from "~/lib/api/types.gen";
import type { TypingPerson } from "~/lib/presence";
import { dockFor, type GateView, type TaskView } from "~/lib/task-view";
import { cn } from "~/lib/utils";
import { ContextMeter } from "./context-meter";
import { MachineControlDock } from "./machine-control-dock";
import { ModelsMeter } from "./models-meter";
import { TypingLine, ViewerNote } from "./presence-bar";
import { StaleSteerStrip, type StaleSteer } from "./stale-steer-strip";
import { COL } from "./transcript";
import type { FollowUpDials } from "./use-follow-up-dials";

type TaskDetailOut = components["schemas"]["TaskDetailOut"];

/**
 * The follow-up box's dials row: the box IS the landing box at rest height - one text line
 * over "+" capture, the size dial (this turn's override; untouched keeps the thread's), and on
 * a FLEET thread its Models seats. No mode dial: mode is thread DNA, inherited by the API (an
 * unchoosable choice is absent).
 */
function FollowUpSettings({ dials }: { dials: FollowUpDials }) {
  const { fleetThread, catalog, suggestion, suggestFlows } = dials;
  return (
    <>
      {fleetThread && (catalog?.length ?? 0) > 0 && (
        <FleetModelsDial
          models={catalog ?? []}
          refs={dials.modelRefs}
          onChange={dials.setModelRefs}
        />
      )}
      {/* Quiet icon = keep the thread's size; a pick - Auto included - re-resolves THIS turn
          (CLI --size parity). */}
      <SizeDial
        scale={dials.turnScale ?? "auto"}
        onChange={dials.setTurnScale}
        fleet={fleetThread}
      />
      {/* Quiet icon = let this turn re-resolve the PATH (fresh kind language re-stamps;
          silence keeps the thread's); a pick forces this turn's kind (CLI --deliverable
          parity). Fleet threads never have a path - the dial is absent, like the mode dial. */}
      {!fleetThread && (
        <DeliverableDial
          value={dials.turnDeliverable ?? "auto"}
          onChange={(v) => dials.setTurnDeliverable(v === "auto" ? null : v)}
        />
      )}
      <ConnectorsControl
        connectors={dials.connectorCatalog ?? []}
        ids={dials.connectorIds}
        onChange={dials.setConnectorIds}
      />
      {suggestion && (
        <ConnectorSuggestionChip
          suggestion={suggestion}
          flow={suggestFlows.flows[suggestion.slug] ?? null}
          onAttach={dials.attachConnector}
          onConnect={(slug) => {
            void suggestFlows.connectFromCatalog(slug).then((row) => {
              if (row?.status === "live") dials.attachConnector(row.id);
            });
          }}
          onDismiss={dials.dismissSuggestion}
        />
      )}
    </>
  );
}

/**
 * ONE gate at a time by law (a single batch); the dock stages it above the box, and the box
 * below stays live - "Or reply directly…" routes its text into the dock's current question.
 * KEYED by the gate: when one resolves and the next takes its place, the dock must remount with
 * fresh state - the same instance carried the previous gate's stepper index, drafts and
 * in-flight latch, which is how a second gate's Skip once arrived pre-disabled.
 */
function GateArea({
  gate,
  detail,
  steerNote,
  spendUsd,
  dockRef,
  onResolved,
  onOpenScreen,
}: {
  gate: GateView;
  detail: TaskDetailOut;
  steerNote: string | null;
  spendUsd: string;
  dockRef: RefObject<QuestionDockHandle | null>;
  onResolved: (resolvedBy?: string, resolvedById?: string | null, action?: string) => void;
  onOpenScreen: () => void;
}) {
  if (dockFor(gate) === "control")
    // A CONTROL gate: everyone gets the line and Open the screen - a viewer watches too - and
    // Skip is an editor's. On a device the creator's Done replaces the screen.
    return (
      <div key={gate.gate_id} className="mb-2">
        <MachineControlDock
          gate={gate}
          machine={detail.machine}
          canEdit={detail.can_edit}
          isCreator={detail.is_creator}
          onOpenScreen={onOpenScreen}
        />
      </div>
    );
  // a viewer sees the question in the tree; only an editor can answer it
  if (!detail.can_edit) return null;
  return (
    <>
      {/* a steer that landed while a question waited applies after the answer */}
      {steerNote && (
        <p className="text-muted-foreground mb-1.5 px-1 text-caption-md-regular" role="status">
          {steerNote}
        </p>
      )}
      {/* The gate's face (`GateDock`): the connect block, Allow/Deny over the evidence, raise
          and resume on "Budget reached" (or wait on the Project's owner), or the question
          stepper. */}
      <GateDock
        key={gate.gate_id}
        ref={dockRef}
        gate={gate}
        onResolved={onResolved}
        // a connector approval runs on the creator's connection: theirs to allow
        isCreator={detail.is_creator}
        creatorName={detail.creator_name}
        budget={{
          taskId: detail.id,
          projectId: detail.project_id ?? null,
          // the LIVE ledger figure, not the page-load snapshot: a stale prefill sits below the
          // real spend and one click would LOWER the ceiling
          spentUsd: Number(spendUsd),
        }}
        className="mb-2"
      />
    </>
  );
}

/**
 * The composer - a FLOATING bar: absolute over the transcript, which scrolls beneath it (its
 * `--bar` padding is this bar's clearance). A background fade lifts the box off the passing
 * text; the box itself goes opaque popover + overlay shadow - the elevation tokens, not a rule,
 * make the edge. pointer-events: the fade must never block clicks on the transcript, only the
 * bar itself is solid.
 */
export function ComposerBar({
  barRef,
  detail,
  view,
  gate,
  live,
  held,
  steerNote,
  spendUsd,
  typers,
  stale,
  sending,
  dials,
  dockRef,
  composerApi,
  placeholder,
  onResolved,
  onOpenScreen,
  onDraft,
  onSubmit,
  onStop,
  onSendAnyway,
  onDiscardStale,
}: {
  barRef: RefObject<HTMLDivElement | null>;
  detail: TaskDetailOut;
  view: TaskView;
  /** The open gate (live-gated), else null. */
  gate: GateView | null;
  live: boolean;
  /** A held fork: live by status, but nothing runs - there is nothing to stop. */
  held: boolean;
  steerNote: string | null;
  spendUsd: string;
  typers: readonly TypingPerson[];
  stale: StaleSteer | null;
  sending: boolean;
  dials: FollowUpDials;
  dockRef: RefObject<QuestionDockHandle | null>;
  composerApi: MutableRefObject<ComposerApi | null>;
  placeholder: string;
  onResolved: (resolvedBy?: string, resolvedById?: string | null, action?: string) => void;
  onOpenScreen: () => void;
  onDraft: (text: string) => void;
  onSubmit: (msg: { text: string; attachments: Attachment[] }) => Promise<void>;
  onStop: () => void;
  onSendAnyway: () => void;
  onDiscardStale: () => void;
}) {
  const canEdit = detail.can_edit;
  // A steer the task moved past while it was typed: what changed, and Send anyway / Discard -
  // the draft stays in the box until one is chosen. Only while the box still holds the refused
  // words: words typed since are a different steer.
  const staleStrip: ReactNode =
    stale && canEdit && live && dials.draft.trim() === stale.text.trim() ? (
      <StaleSteerStrip
        stale={stale}
        busy={sending}
        onSendAnyway={onSendAnyway}
        onDiscard={onDiscardStale}
        className="mb-2"
      />
    ) : null;
  return (
    <div
      ref={barRef}
      className="from-background via-background/85 pointer-events-none absolute inset-x-0 bottom-0 z-10 bg-gradient-to-t to-transparent px-gutter pt-10 pb-3"
    >
      <div className={cn(COL, "pointer-events-auto")}>
        {gate && (
          <GateArea
            gate={gate}
            detail={detail}
            steerNote={steerNote}
            spendUsd={spendUsd}
            dockRef={dockRef}
            onResolved={onResolved}
            onOpenScreen={onOpenScreen}
          />
        )}
        {staleStrip}
        {/* who else is mid-keystroke - "Bob is steering…" - one quiet line above the box */}
        <TypingLine people={typers} />
        {!canEdit ? (
          // A viewer reads along; only editors write. No box, no typing frames.
          <ViewerNote />
        ) : (
          <Composer
            className="[&_[data-slot=input-group]]:bg-popover [&_[data-slot=input-group]]:shadow-(--shadow-overlay) dark:[&_[data-slot=input-group]]:bg-popover"
            apiRef={composerApi}
            onTextChange={onDraft}
            onSubmit={onSubmit}
            status={sending ? "submitting" : "idle"}
            // Stop renders BESIDE the send disc while the task runs - steering needs a visible
            // send, so the disc never becomes the stop.
            onStop={live && !held ? onStop : undefined}
            allowAttachments={!live}
            placeholder={placeholder}
            // A steer is a directive, so it keeps the single-line chat layout.
            settings={live ? undefined : <FollowUpSettings dials={dials} />}
          />
        )}
        {/* the bar's last line, right-aligned: two quiet gauges - which models ran (and where
            the spend went), and what this task was told beyond the ask. Each glyph's popover
            is its receipt. */}
        <div className="mt-1 flex items-center justify-end gap-1">
          <ModelsMeter view={view} verifyUsd={view.verifyUsd} />
          <ContextMeter context={view.context} />
        </div>
      </div>
    </div>
  );
}
