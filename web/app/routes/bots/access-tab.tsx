/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The Access tab (ADR-056) - one clear page for what this bot can touch, why, and at what
// permission level, resolved FOR THE VIEWER. Three quiet sections: tools with their one-line
// meanings, skill pins, and the declared connectors as cards wearing the author's why, MY
// readiness, and MY effective policy rollup. Editing policies stays on /connectors (the
// one-home law) - every card deep-links into its peek.

import { useEffect, useState } from "react";
import { Link } from "react-router";
import { EmptyState, LoadingState, Pill, Rows, Section } from "~/components/kit";
import { ConnectorIcon } from "~/components/connectors/connector-icon";
import { Badge } from "@makeplane/propel/components/badge";
import { api, toastError, unwrap } from "~/lib/api/client";
import type { components } from "~/lib/api/types.gen";

type BotOut = components["schemas"]["BotOut"];
type AccessOut = components["schemas"]["BotAccessOut"];
type ConnectorAccess = components["schemas"]["BotConnectorAccessOut"];

/** The grant vocabulary's one-line meanings - copy, so it lives beside the pixels. */
const TOOL_MEANING: Record<string, string> = {
  "frame.read_file": "Read files in its sandbox",
  "frame.write_file": "Write files in its sandbox",
  "frame.shell": "Run commands in its sandbox",
  "frame.publish": "Publish deliverables",
  "media.generate_image": "Generate images",
  "web.fetch": "Read public web pages",
  "web.search": "Search the web",
  "routine.create": "Propose standing work, born paused",
};

const STATUS_COPY: Record<
  string,
  { label: string; tone: "success" | "warning" | "danger" | "muted" }
> = {
  connected: { label: "Connected", tone: "success" },
  needs_auth: { label: "Needs authorization", tone: "warning" },
  disabled: { label: "Disabled", tone: "muted" },
  needs_connect: { label: "Not connected", tone: "warning" },
  custom: { label: "Bring your own", tone: "muted" },
};

export function AccessTab({ bot }: { bot: BotOut }) {
  const [access, setAccess] = useState<AccessOut | undefined>(undefined);
  useEffect(() => {
    let alive = true;
    setAccess(undefined);
    if (bot.connectors.length === 0) {
      setAccess({ connectors: [] });
      return;
    }
    unwrap(
      api.GET("/v1/bots/{name}/access", {
        params: { path: { name: bot.name }, query: { project_id: bot.project_id ?? undefined } },
      }),
    ).then(
      (d) => alive && setAccess(d),
      (e) => alive && toastError(e, "Couldn't load access."),
    );
    return () => {
      alive = false;
    };
  }, [bot.name, bot.project_id, bot.connectors.length]);

  return (
    <div className="scrollbar-thin min-h-0 flex-1 space-y-6 overflow-y-auto px-6 py-5">
      <Section title="Tools">
        {bot.tools.length === 0 && !bot.can_ask_user ? (
          <p className="text-muted-foreground px-1 text-body-sm-regular">
            No tools - this bot only reads and writes text.
          </p>
        ) : (
          <Rows as="ul">
            {bot.tools.map((t) => (
              <li key={t} className="flex items-center gap-3 px-4 py-2.5 text-body-sm-regular">
                <code className="text-caption-md-regular">{t}</code>
                <span className="text-muted-foreground ml-auto text-caption-md-regular">
                  {TOOL_MEANING[t] ?? "Granted"}
                </span>
              </li>
            ))}
            {bot.can_ask_user && (
              <li className="flex items-center gap-3 px-4 py-2.5 text-body-sm-regular">
                <code className="text-caption-md-regular">ask_user</code>
                <span className="text-muted-foreground ml-auto text-caption-md-regular">
                  Ask you questions mid-task
                </span>
              </li>
            )}
          </Rows>
        )}
      </Section>

      {bot.skills.length > 0 && (
        <Section title="Skills">
          <div className="flex flex-wrap gap-1.5 px-1">
            {bot.skills.map((s) => (
              <Badge key={s} variant="grey" size="xs" label={s} />
            ))}
          </div>
        </Section>
      )}

      <Section title="Connectors">
        {access === undefined ? (
          <LoadingState label="Checking your connections…" />
        ) : access.connectors.length === 0 ? (
          <EmptyState
            title="No connectors declared"
            description="This bot works from its sandbox and the web door alone."
          />
        ) : (
          <>
            <Rows as="ul">
              {access.connectors.map((c) => (
                <ConnectorRow key={c.slug} c={c} />
              ))}
            </Rows>
            <p className="text-muted-foreground pt-2 text-caption-md-regular">
              Statuses are yours: everyone runs this bot on their own connections, and credentials
              never travel with it. Permissions are edited on the connectors page.
            </p>
          </>
        )}
      </Section>
    </div>
  );
}

function ConnectorRow({ c }: { c: ConnectorAccess }) {
  const status = STATUS_COPY[c.status] ?? STATUS_COPY.custom;
  const rollup =
    c.tools_allow + c.tools_ask + c.tools_off > 0
      ? [
          c.tools_allow > 0 ? `${c.tools_allow} allow` : null,
          c.tools_ask > 0 ? `${c.tools_ask} ask` : null,
          c.tools_off > 0 ? `${c.tools_off} off` : null,
        ]
          .filter(Boolean)
          .join(" · ")
      : null;
  return (
    <li>
      <Link
        to={`/connectors?connector=${c.slug}`}
        className="hover:bg-accent/40 flex w-full items-start gap-3 px-4 py-3 text-left text-body-sm-regular transition-colors"
      >
        <ConnectorIcon slug={c.slug} size="sm" className="mt-0.5" />
        <span className="min-w-0 flex-1">
          <span className="flex items-center gap-2">
            <span className="truncate font-medium">{c.display_name}</span>
            <Pill tone={status.tone}>{status.label}</Pill>
          </span>
          {c.why && (
            <span className="text-muted-foreground block truncate text-caption-md-regular">
              {c.why}
            </span>
          )}
          {rollup && (
            <span className="text-muted-foreground block text-caption-md-regular tabular-nums">
              {rollup}
            </span>
          )}
        </span>
      </Link>
    </li>
  );
}
