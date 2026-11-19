/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The template preview (ADR-056) - the share link's landing, readable LOGGED OUT.
// Everything shown here installs verbatim: full instructions included, because a stranger
// deserves to read exactly the judgment they are adding. The honest strip states the
// posture; the one CTA installs (or routes through login and comes straight back).

import { useState } from "react";
import { Link, useNavigate } from "react-router";
import { toast } from "@prismoid/blocks/toast";
import { BotAvatar } from "~/components/icon-picker";
import { EmptyState, Pill, QuietDisclosure, Rows, Section } from "~/components/kit";
import { ConnectorIcon } from "~/components/connectors/connector-icon";
import { Badge } from "@makeplane/propel/components/badge";
import { Button } from "@makeplane/propel/components/button";
import { Input, InputGroup } from "@makeplane/propel/components/input";
import { api, getToken, toastError, unwrap } from "~/lib/api/client";
import { ago } from "~/lib/format";
import type { Route } from "./+types/route";

export function meta() {
  return [{ title: "Template · Prismoid" }, { name: "robots", content: "noindex" }];
}

export async function clientLoader({ params }: Route.ClientLoaderArgs) {
  try {
    const template = await unwrap(
      api.GET("/v1/templates/bots/{slug}", { params: { path: { slug: params.slug } } }),
    );
    return { template, authed: Boolean(getToken()) };
  } catch {
    return { template: null, authed: Boolean(getToken()) };
  }
}
clientLoader.hydrate = true as const;

export default function TemplatePreview({ loaderData, params }: Route.ComponentProps) {
  const { template, authed } = loaderData;
  const navigate = useNavigate();
  const [busy, setBusy] = useState(false);
  const [rename, setRename] = useState<string | null>(null);

  if (!template) {
    return (
      <div className="flex min-h-svh items-center justify-center">
        <EmptyState
          title="This template is gone"
          description="The link was unpublished, or it never existed. Ask the person who shared it."
        />
      </div>
    );
  }

  async function install() {
    if (!template) return;
    setBusy(true);
    try {
      const out = await unwrap(
        api.POST("/v1/templates/bots/{slug}/install", {
          params: { path: { slug: params.slug } },
          body: rename ? { rename } : {},
        }),
      );
      toast.success(`Installed as ${out.name} - a draft of your own. Test it, then publish.`);
      navigate(`/bots/${out.name}?tab=configure`);
    } catch (e) {
      const msg = String((e as Error)?.message ?? "");
      if (msg.includes("already have a personal bot")) {
        setRename(rename ?? `${template.name}-2`);
        toastError(e, "That name is taken - pick another below.");
      } else {
        toastError(e, "Couldn't install the template.");
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto w-full max-w-2xl space-y-6 px-6 py-10">
      <div className="flex items-start gap-4">
        <BotAvatar name={template.name} icon={template.icon} hue={template.icon_hue} size="lg" />
        <div className="min-w-0 flex-1">
          <h1 className="font-heading truncate text-h4-regular font-semibold">
            {template.title || template.name}
          </h1>
          <p className="text-muted-foreground text-body-sm-regular">
            {template.name} · template v{template.template_version} · by {template.author_name} ·{" "}
            {ago(template.created_at)}
            {template.install_count > 0 &&
              ` · installed ${template.install_count} ${template.install_count === 1 ? "time" : "times"}`}
          </p>
        </div>
      </div>

      <p className="text-body-sm-regular">{template.description}</p>

      {/* The honest strip - the whole trust posture in three sentences. */}
      <p className="text-muted-foreground bg-muted/40 rounded-md px-4 py-3 text-caption-md-regular">
        Community template. Plane has not reviewed it. It installs as a draft: nothing runs until
        you test and publish it. Templates can never carry logins: you connect your own services.
      </p>

      <div className="flex items-center gap-2">
        {authed ? (
          <Button
            variant="primary"
            size="md"
            stretch="auto"
            label="Add to your bots"
            onClick={() => void install()}
            disabled={busy}
          />
        ) : (
          <Button
            nativeButton={false}
            variant="primary"
            size="md"
            stretch="auto"
            label="Sign in to install"
            render={<Link to={`/login?next=/t/bots/${params.slug}`} />}
          />
        )}
        {rename !== null && (
          <div className="w-56">
            <InputGroup size="md">
              <Input
                size="md"
                value={rename}
                onChange={(e) => setRename(e.currentTarget.value)}
                aria-label="Install under a different name"
              />
            </InputGroup>
          </div>
        )}
      </div>

      {(template.tools.length > 0 || template.skills.length > 0) && (
        <Section title="What it can touch">
          <div className="flex flex-wrap gap-1.5 px-1">
            <Badge variant="neutral" size="xs" label={template.capability} />
            <Badge variant="neutral" size="xs" label={`publishes ${template.produces}`} />
            {template.tools.map((t) => (
              <Badge key={t} variant="grey" size="xs" label={t} />
            ))}
            {template.skills.map((s) => (
              <Badge key={s} variant="grey" size="xs" label={`skill · ${s}`} />
            ))}
          </div>
        </Section>
      )}

      {template.connectors.length > 0 && (
        <Section title="Connectors it will ask for">
          <Rows as="ul">
            {template.connectors.map((slug) => (
              <li key={slug} className="flex items-start gap-3 px-4 py-3 text-body-sm-regular">
                <ConnectorIcon slug={slug} size="sm" className="mt-0.5" />
                <span className="min-w-0 flex-1">
                  <span className="block font-medium">{slug}</span>
                  {template.connector_why[slug] && (
                    <span className="text-muted-foreground block text-caption-md-regular">
                      {template.connector_why[slug]}
                    </span>
                  )}
                </span>
              </li>
            ))}
          </Rows>
          <p className="text-muted-foreground pt-2 text-caption-md-regular">
            It runs on YOUR connections: nothing of the author's travels with it.
          </p>
        </Section>
      )}

      <Section title="Its instructions, in full">
        <QuietDisclosure label="Read the judgment you are installing" defaultOpen>
          <pre className="text-muted-foreground max-h-96 overflow-auto rounded-md p-4 text-caption-md-regular whitespace-pre-wrap">
            {template.instructions}
          </pre>
        </QuietDisclosure>
      </Section>

      {template.cases.length > 0 && (
        <Section title="Its selection evals">
          <Rows as="ul">
            {template.cases.map((c, i) => (
              <li key={i} className="flex items-center gap-3 px-4 py-2.5 text-body-sm-regular">
                <span className="min-w-0 flex-1 truncate">{c.goal}</span>
                <Pill tone={c.expect === template.name ? "success" : "muted"}>
                  {c.expect === template.name ? "wins" : "refuses"}
                </Pill>
              </li>
            ))}
          </Rows>
          <p className="text-muted-foreground pt-2 text-caption-md-regular">
            Verdicts never travel: you re-earn the green before publishing your copy.
          </p>
        </Section>
      )}
    </div>
  );
}

export { RouteErrorBoundary as ErrorBoundary } from "~/components/layout/route-error";
