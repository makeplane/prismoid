/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// Publishing a bot as a template others can start from: the byline, the URL acknowledgement,
// and the copyable link once it is out.

import { useState } from "react";
import { toast } from "@prismoid/blocks/toast";
import { CopyButton, Rows, Section } from "~/components/kit";
import { Button } from "@makeplane/propel/components/button";
import { Input, InputGroup } from "@makeplane/propel/components/input";
import { useLoad } from "~/hooks/use-load";
import { ApiError, api, toastError, unwrap } from "~/lib/api/client";
import type { components } from "~/lib/api/types.gen";

type BotOut = components["schemas"]["BotOut"];

/** Share as template: the bot travels as a link - identity, routing, skills,
 *  connector slugs and cases; never credentials, history, or verdicts. Republishing the same
 *  name ships an update under the same slug. The 422s here are the safety nets talking:
 *  secret shapes refuse outright, external URLs list themselves for acknowledgement. */
export function ShareTemplate({ head }: { head: BotOut }) {
  const [mine, setMine] = useState<
    | {
        slug: string;
        template_version: number;
        install_count: number;
        status: string;
      }
    | null
    | undefined
  >(undefined);
  const [byline, setByline] = useState("");
  const [ackUrls, setAckUrls] = useState<string[] | null>(null);
  const [busy, setBusy] = useState(false);

  useLoad((signal) => unwrap(api.GET("/v1/templates/bots", { signal })), [head.name], {
    onData: (rows) =>
      setMine(rows.find((r) => r.name === head.name && r.status === "published") ?? null),
    onError: () => setMine(null),
  });

  async function publish() {
    if (!byline.trim()) return;
    setBusy(true);
    try {
      const row = await unwrap(
        api.POST("/v1/templates/bots", {
          body: {
            name: head.name,
            project_id: head.project_id,
            author_name: byline.trim(),
            acknowledge_urls: ackUrls !== null,
          },
        }),
      );
      setMine({
        slug: row.slug,
        template_version: row.template_version,
        install_count: row.install_count,
        status: row.status,
      });
      setAckUrls(null);
      toast.success(`Shared as v${row.template_version}. Anyone with the link can install it.`);
    } catch (e) {
      // The envelope names the URLs in `fields.urls` (a comma-joined list); never the
      // message text, whose URLs carry their own colons.
      const urls = e instanceof ApiError ? e.fields?.urls : undefined;
      if (urls) {
        setAckUrls(
          urls
            .split(",")
            .map((u) => u.trim())
            .filter(Boolean),
        );
        toastError(e, "Review the URLs below, then share again to acknowledge them.");
      } else {
        toastError(e, "Couldn't share it."); // the secret net's 422 names what it saw
      }
    } finally {
      setBusy(false);
    }
  }

  async function unshare() {
    if (!mine) return;
    try {
      await unwrap(
        api.POST("/v1/templates/bots/{slug}/unpublish", {
          params: { path: { slug: mine.slug } },
        }),
      );
      setMine(null);
      toast.success("Unlisted - the link is dead. Installed copies stand.");
    } catch (e) {
      toastError(e, "Couldn't unshare it.");
    }
  }

  return (
    <Section title="Share as template">
      <Rows as="div">
        <div className="space-y-2 px-4 py-3.5">
          {mine === undefined ? null : mine ? (
            <>
              <div className="flex items-center gap-2 text-body-sm-regular">
                <code className="text-caption-md-regular">/t/bots/{mine.slug}</code>
                <CopyButton
                  value={`${window.location.origin}/t/bots/${mine.slug}`}
                  label="Copy the share link"
                />
                <span className="text-muted-foreground ml-auto text-caption-md-regular tabular-nums">
                  v{mine.template_version}
                  {mine.install_count > 0 && ` · installed ${mine.install_count}×`}
                </span>
              </div>
              <div className="flex items-center gap-2">
                <div className="w-56">
                  <InputGroup size="md">
                    <Input
                      size="md"
                      value={byline}
                      onChange={(e) => setByline(e.currentTarget.value)}
                      placeholder="Your byline for the update"
                      aria-label="Byline"
                    />
                  </InputGroup>
                </div>
                <Button
                  variant="secondary"
                  size="sm"
                  stretch="auto"
                  label="Publish update"
                  onClick={() => void publish()}
                  disabled={busy || !byline.trim()}
                />
                <Button
                  variant="ghost"
                  size="sm"
                  stretch="auto"
                  label="Unshare"
                  onClick={() => void unshare()}
                />
              </div>
            </>
          ) : (
            <>
              <p className="text-muted-foreground text-caption-md-regular">
                What travels: identity, routing, instructions, skills and connector NAMES, and the
                eval cases. What never travels: logins, history, verdicts, spend. The link opens
                logged out; installs land as the other person's own draft.
              </p>
              <div className="flex items-center gap-2">
                <div className="w-56">
                  <InputGroup size="md">
                    <Input
                      size="md"
                      value={byline}
                      onChange={(e) => setByline(e.currentTarget.value)}
                      placeholder="Shown as the author, e.g. Vamsi"
                      aria-label="Byline"
                    />
                  </InputGroup>
                </div>
                <Button
                  variant="primary"
                  size="sm"
                  stretch="auto"
                  label="Share as template"
                  onClick={() => void publish()}
                  disabled={busy || !byline.trim()}
                />
              </div>
            </>
          )}
          {ackUrls && (
            <div className="text-muted-foreground text-caption-md-regular">
              <p>External URLs in this bot's text (shown as plain text on the preview):</p>
              <ul className="list-inside list-disc">
                {ackUrls.map((u) => (
                  <li key={u} className="truncate">
                    {u}
                  </li>
                ))}
              </ul>
              <p>Sharing again publishes with these acknowledged.</p>
            </div>
          )}
        </div>
      </Rows>
    </Section>
  );
}
