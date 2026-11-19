/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The bot population, observed - one row per bot HEAD across every user and project, a
// ResourceList config: identity + version + lifecycle on the name line, author · scope ·
// description beneath. Read-only by design (bots belong to their authors; the
// publish gate is the control surface, not this page). The status chips are the one
// filter the wire supports; search matches the bot's name. A row opens the bot's
// detail page - whose instructions read is audited server-side.

import { Bot as BotIcon } from "lucide-react";
import { BOT_STATUS_TONE } from "~/components/admin/bot-copy";
import { ResourceList, type ResourceListConfig } from "~/components/admin/resource-list";
import { IconTile, MetaChip, Pill } from "~/components/kit";
import { Badge } from "@makeplane/propel/components/badge";
import { api, unwrap } from "~/lib/api/client";
import type { components } from "~/lib/api/types.gen";

type BotRow = components["schemas"]["AdminBotOut"];

export function meta() {
  return [{ title: "Bots · Admin · Prismoid" }];
}

const config: ResourceListConfig<BotRow> = {
  fetchPage: ({ cursor, q, filters, limit }) =>
    unwrap(
      api.GET("/v1/admin/bots", {
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
  orderKey: (b) => b.created_at,
  rowKey: (b) => b.id,
  rowTo: (b) => `/bots/${b.id}`,
  row: (b) => (
    <>
      <IconTile icon={BotIcon} />
      <span className="min-w-0 flex-1">
        <span className="flex flex-wrap items-center gap-2">
          <span className="truncate text-body-sm-regular font-medium">{b.name}</span>
          <Badge variant="neutral" size="xs" label={`v${b.version}`} />
          <Pill tone={BOT_STATUS_TONE[b.status] ?? "muted"} className="capitalize">
            {b.status}
          </Pill>
          <MetaChip>evals {b.eval_status}</MetaChip>
        </span>
        <span className="text-muted-foreground block truncate text-caption-md-regular">
          {b.author ? b.author.email : "no author"} · {b.project_id ? "project" : "personal"}
          {b.description && ` · ${b.description}`}
        </span>
      </span>
    </>
  ),
  search: { placeholder: "Search bot name…", topbar: true },
  filters: [
    {
      param: "status",
      options: [
        { value: "draft", label: "Draft" },
        { value: "published", label: "Published" },
        { value: "archived", label: "Archived" },
      ],
    },
  ],
  empty: {
    icon: BotIcon,
    title: "No bots",
    description: "User-authored agents appear here as people create them.",
  },
};

export default function AdminBots() {
  return <ResourceList {...config} />;
}

export { RouteErrorBoundary as ErrorBoundary } from "~/components/layout/route-error";
