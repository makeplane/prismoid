/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// One bot, observed - a read-only detail page: the operator reads what the bot IS (its
// description and instructions) and how it got here (its versions), and changes nothing -
// bots belong to their authors, so no verbs render (capability law: absent, not disabled).
// Opening this page reads the instructions, and the SERVER records that read in the audit
// trail (bot.view - content-reads are acts, like the task peek); the page says so quietly.
// Two tabs, tab in the URL (?tab=, overview default): Overview (description + the
// instructions block - kit Markdown when they carry structure, honest pre-wrap otherwise)
// and Versions (the head marked, newest first as the wire orders them).

import { Badge } from "@makeplane/propel/components/badge";
import { Bot as BotIcon } from "lucide-react";
import { Link, useLoaderData, type ClientLoaderFunctionArgs } from "react-router";
import { revalidateOnPageChange } from "~/lib/pending-route";
import { BOT_EVAL_TONE, BOT_STATUS_TONE } from "~/components/admin/bot-copy";
import { looksLikeMarkdown } from "~/components/admin/markdownish";
import { IconTile, Markdown, MetaChip, PageHeader, Pill, Rows, Section } from "~/components/kit";
import { Tab as TabItem, Tabs, TabsList } from "@makeplane/propel/components/tabs";
import { api, unwrap } from "~/lib/api/client";
import { ago } from "~/lib/format";
import { useUrlTab } from "~/lib/url-state";

export function meta({ loaderData }: { loaderData?: { bot?: { name?: string } } }) {
  return [
    {
      title: loaderData?.bot ? `${loaderData.bot.name} · Admin · Prismoid` : "Bot · Admin",
    },
  ];
}

export async function clientLoader({ params }: ClientLoaderFunctionArgs) {
  const bot = await unwrap(
    api.GET("/v1/admin/bots/{bot_id}", {
      params: { path: { bot_id: params.id as string } },
    }),
  );
  return { bot };
}
clientLoader.hydrate = true as const;

// A tab, a filter or an open peek rides the query string: the page already holds its data, so
// the router does not hold that click for another fetch.
export const shouldRevalidate = revalidateOnPageChange;

// The breadcrumb says WHICH bot, not "Detail".
export const handle = {
  crumb: (data: unknown) => (data as { bot?: { name?: string } } | undefined)?.bot?.name ?? null,
};

const TABS = ["overview", "versions"] as const;
type Tab = (typeof TABS)[number];

export default function AdminBotDetail() {
  const { bot } = useLoaderData<typeof clientLoader>();

  const [tab, setTab] = useUrlTab(TABS);

  return (
    <div className="space-y-6">
      <PageHeader
        leading={<IconTile icon={BotIcon} className="size-10" />}
        title={bot.name}
        description={
          <span className="text-muted-foreground flex flex-wrap items-center gap-2 text-body-sm-regular">
            <Badge variant="neutral" size="xs" label={`v${bot.version}`} />
            <Pill tone={BOT_STATUS_TONE[bot.status] ?? "muted"} className="capitalize">
              {bot.status}
            </Pill>
            <Pill tone={BOT_EVAL_TONE[bot.eval_status] ?? "muted"}>evals {bot.eval_status}</Pill>
            {bot.author ? (
              <Link
                to={`/users/${bot.author.id}`}
                className="hover:text-foreground hover:underline"
              >
                {bot.author.email}
              </Link>
            ) : (
              "no author"
            )}
            ·{" "}
            {bot.project_id ? (
              <Link
                to={`/projects/${bot.project_id}`}
                className="hover:text-foreground hover:underline"
              >
                project
              </Link>
            ) : (
              "personal"
            )}
            · created {ago(bot.created_at)}
          </span>
        }
      />

      {/* The negative margin sets the first label's text on the page edge. */}
      <div className="-ml-2.5">
        <Tabs variant="underline" value={tab} onValueChange={(v) => setTab(v as Tab)}>
          <TabsList>
            <TabItem value="overview" label="Overview" />
            <TabItem value="versions" label="Versions" />
          </TabsList>
        </Tabs>
      </div>

      {tab === "overview" && (
        <>
          {bot.description && <p className="text-body-sm-regular">{bot.description}</p>}
          <Section title="Instructions">
            <Rows as="div">
              <div className="px-4 py-3.5">
                {looksLikeMarkdown(bot.instructions) ? (
                  <Markdown className="text-body-sm-regular">{bot.instructions}</Markdown>
                ) : (
                  <p className="text-body-sm-regular whitespace-pre-wrap">{bot.instructions}</p>
                )}
              </div>
            </Rows>
            <p className="text-muted-foreground text-caption-md-regular">
              Reading a bot's instructions is recorded in the audit trail.
            </p>
          </Section>
        </>
      )}

      {tab === "versions" && (
        <Rows as="ul">
          {bot.versions.map((v) => (
            <li key={v.id} className="flex flex-wrap items-center gap-3 px-4 py-3">
              <Badge variant="neutral" size="xs" label={`v${v.version}`} />
              <span className="text-muted-foreground min-w-0 flex-1 truncate text-caption-md-regular">
                created {ago(v.created_at)}
              </span>
              <MetaChip>evals {v.eval_status}</MetaChip>
              <Pill tone={BOT_STATUS_TONE[v.status] ?? "muted"} className="capitalize">
                {v.status}
              </Pill>
              {v.version === bot.version && <Badge variant="neutral" size="xs" label="head" />}
            </li>
          ))}
        </Rows>
      )}
    </div>
  );
}

export { RouteErrorBoundary as ErrorBoundary } from "~/components/layout/route-error";
