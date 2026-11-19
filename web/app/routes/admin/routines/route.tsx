/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// Standing work, observed and stoppable (ADR-048): every routine across every owner, the
// engine's list anatomy, and ONE verb - the audited pause (ReasonDialog, stopPropagation
// under the row: the operator investigates in place). Resume stays the OWNER's act.

import { AlarmClock, Pause } from "lucide-react";
import { toast } from "@prismoid/blocks/toast";
import { ReasonDialog } from "~/components/admin/confirm";
import { ResourceList, type ResourceListConfig } from "~/components/admin/resource-list";
import { Icon } from "@makeplane/propel/components/icon";
import { IconButton } from "@makeplane/propel/components/icon-button";
import { Pill } from "~/components/kit";
import { api, toastError, unwrap } from "~/lib/api/client";
import { ago, inAgo } from "~/lib/format";
import type { components } from "~/lib/api/types.gen";

type RoutineRow = components["schemas"]["AdminRoutineOut"];

export function meta() {
  return [{ title: "Routines · Admin · Prismoid" }];
}

const STATUS_TONE = { active: "success", paused: "warning", retired: "muted" } as const;

function PauseVerb({ r }: { r: RoutineRow }) {
  return (
    // Only stops the row's click; the dialog's own trigger is the control.
    <span role="presentation" onClick={(e) => e.stopPropagation()}>
      <ReasonDialog
        trigger={
          <IconButton
            variant="ghost"
            size="sm"
            icon={<Icon icon={Pause} />}
            aria-label={`Pause ${r.name}`}
          />
        }
        title={`Pause “${r.name}”?`}
        description="Its clock and doors stop immediately; the owner is told, with your reason, and only they can resume it."
        placeholder="Why is the operator stopping this routine?"
        confirmLabel="Pause routine"
        onConfirm={async (reason) => {
          try {
            await unwrap(
              api.POST("/v1/admin/routines/{routine_id}/pause", {
                params: { path: { routine_id: r.id } },
                body: { reason },
              }),
            );
            toast.success("Paused. Audited, and the owner has the note.");
          } catch (e) {
            toastError(e, "Couldn't pause it.");
            throw e;
          }
        }}
      />
    </span>
  );
}

const config: ResourceListConfig<RoutineRow> = {
  fetchPage: ({ cursor, q, filters, limit }) =>
    unwrap(
      api.GET("/v1/admin/routines", {
        params: {
          query: {
            limit,
            ...(q ? { q } : {}),
            ...(filters.status ? { status: filters.status } : {}),
            ...(cursor ? { cursor } : {}),
          },
        },
      }),
    ),
  orderKey: (r) => r.created_at,
  rowKey: (r) => r.id,
  onRow: () => undefined, // observation rows; the one verb rides inline
  row: (r) => (
    <>
      <AlarmClock className="text-muted-foreground size-4 shrink-0" />
      <span className="min-w-0 flex-1">
        <span className="block truncate text-body-sm-regular font-medium">{r.name}</span>
        <span className="text-muted-foreground block truncate text-caption-md-regular">
          {r.owner_email ?? "erased account"} · {r.trigger_kind}
          {r.keeper !== "one" ? ` · kept by ${r.keeper}` : ""}
          {r.status === "paused" && r.pause_note ? ` · ${r.paused_reason}: ${r.pause_note}` : ""}
        </span>
      </span>
      {r.consecutive_failures > 0 && <Pill tone="warning">{r.consecutive_failures} failing</Pill>}
      {r.next_fire_at && r.status === "active" && (
        <span className="text-muted-foreground text-caption-md-regular tabular-nums">
          {inAgo(r.next_fire_at)}
        </span>
      )}
      <span className="text-muted-foreground text-caption-md-regular tabular-nums">
        {r.runs_minted} run{r.runs_minted === 1 ? "" : "s"}
        {r.last_fired_at ? ` · last ${ago(r.last_fired_at)}` : ""}
      </span>
      <Pill tone={STATUS_TONE[r.status as keyof typeof STATUS_TONE] ?? "muted"}>{r.status}</Pill>
      {r.status === "active" && <PauseVerb r={r} />}
    </>
  ),
  search: { placeholder: "Search routine names…", topbar: true },
  filters: [
    {
      param: "status",
      options: [
        { value: "active", label: "Active" },
        { value: "paused", label: "Paused" },
        { value: "retired", label: "Retired" },
      ],
    },
  ],
  empty: {
    icon: AlarmClock,
    title: "No routines",
    description: "Standing work appears here as people save it.",
  },
};

export default function AdminRoutines() {
  return <ResourceList {...config} />;
}

export { RouteErrorBoundary as ErrorBoundary } from "~/components/layout/route-error";
