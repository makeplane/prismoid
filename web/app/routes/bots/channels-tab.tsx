/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The Channels tab (ADR-056) - this bot's external doors. Webhook first: mint a door,
// the bc_ token shows ONCE (the API-keys ceremony), callers POST messages and poll replies
// with it. The input-mode dial names its risk in place; every refusal lands on the ledger
// with a reason (`prismoid channels log`).

import { Plus, RefreshCw, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "@prismoid/blocks/toast";
import { CopyButton, EmptyState, LoadingState, Pill, Rows } from "~/components/kit";
import { Button } from "@makeplane/propel/components/button";
import { Icon } from "@makeplane/propel/components/icon";
import { IconButton } from "@makeplane/propel/components/icon-button";
import { api, toastError, unwrap } from "~/lib/api/client";
import { ago } from "~/lib/format";
import type { components } from "~/lib/api/types.gen";

type ChannelOut = components["schemas"]["ChannelOut"];

const STATUS_TONE = { active: "success", disabled: "muted", broken: "danger" } as const;

export function ChannelsTab({ name, projectId }: { name: string; projectId: string | null }) {
  const [rows, setRows] = useState<ChannelOut[] | undefined>(undefined);
  const [minted, setMinted] = useState<{ id: string; token: string } | null>(null);

  async function reload() {
    try {
      setRows(await unwrap(api.GET("/v1/bots/{name}/channels", { params: { path: { name } } })));
    } catch (e) {
      toastError(e, "Couldn't load the channels.");
      setRows([]);
    }
  }
  useEffect(() => {
    void reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- reload on a bot change only; reload is a fresh closure each render
  }, [name]);

  async function addWebhook() {
    try {
      const row = await unwrap(
        api.POST("/v1/bots/{name}/channels", {
          params: { path: { name } },
          body: { kind: "webhook", project_id: projectId, label: `${name} webhook` },
        }),
      );
      setMinted(row.token ? { id: row.id, token: row.token } : null);
      await reload();
    } catch (e) {
      toastError(e, "Couldn't mint the channel.");
    }
  }

  async function verb(id: string, action: "disable" | "enable") {
    try {
      await unwrap(
        api.POST(`/v1/channels/{channel_id}/${action}` as "/v1/channels/{channel_id}/disable", {
          params: { path: { channel_id: id } },
        }),
      );
      await reload();
    } catch (e) {
      toastError(e, "Couldn't change the channel.");
    }
  }

  async function rotate(id: string) {
    try {
      const row = await unwrap(
        api.POST("/v1/channels/{channel_id}/token", { params: { path: { channel_id: id } } }),
      );
      setMinted(row.token ? { id, token: row.token } : null);
      toast.success("Rotated - the old token died this instant.");
    } catch (e) {
      toastError(e, "Couldn't rotate the token.");
    }
  }

  async function remove(id: string) {
    try {
      await unwrap(
        api.DELETE("/v1/channels/{channel_id}", { params: { path: { channel_id: id } } }),
      );
      toast.success("Channel removed, ledger and all.");
      await reload();
    } catch (e) {
      toastError(e, "Couldn't remove it.");
    }
  }

  return (
    <div className="scrollbar-thin min-h-0 flex-1 space-y-4 overflow-y-auto px-6 py-5">
      <div className="flex items-center justify-between">
        <p className="text-muted-foreground text-body-sm-regular">
          Doors into {name} from outside: any system that can POST JSON, with the channel's own
          token. Every message and refusal lands on the ledger with its reason.
        </p>
        <Button
          variant="primary"
          size="sm"
          stretch="auto"
          icon={<Icon icon={Plus} />}
          label="Add webhook"
          onClick={() => void addWebhook()}
        />
      </div>

      {minted && (
        <div className="bg-muted/40 space-y-1 rounded-md px-4 py-3 text-body-sm-regular">
          <p className="font-medium">Token, shown once - store it where the caller lives:</p>
          <p className="flex items-center gap-2">
            <code className="min-w-0 flex-1 truncate text-caption-md-regular">{minted.token}</code>
            <CopyButton value={minted.token} label="Copy the token" />
          </p>
          <p className="text-muted-foreground text-caption-md-regular">
            POST /v1/channels/{minted.id}/messages with it as the Bearer; GET the same path to read
            replies. It opens this channel and nothing else.
          </p>
        </div>
      )}

      {rows === undefined ? (
        <LoadingState label="Loading channels…" />
      ) : rows.length === 0 ? (
        <EmptyState
          title="No doors yet"
          description="Mint a webhook to talk to this bot from any other system. Email and Telegram arrive next."
        />
      ) : (
        <Rows as="ul">
          {rows.map((r) => (
            <li key={r.id} className="flex items-center gap-3 px-4 py-3 text-body-sm-regular">
              <span className="min-w-0 flex-1">
                <span className="flex items-center gap-2">
                  <span className="truncate font-medium">{r.label || r.kind}</span>
                  <Pill tone={STATUS_TONE[r.status as keyof typeof STATUS_TONE] ?? "muted"}>
                    {r.status}
                  </Pill>
                </span>
                <span className="text-muted-foreground block text-caption-md-regular">
                  {r.kind} · ${Number(r.budget_usd_per_turn).toFixed(2)}/turn ·{" "}
                  {r.daily_message_cap}/day
                  {r.input_mode === "instruction" && " · messages steer (you opted in)"}
                  {r.last_message_at && ` · last ${ago(r.last_message_at)}`}
                </span>
                {r.last_error && (
                  <span className="text-destructive block truncate text-caption-md-regular">
                    {r.last_error}
                  </span>
                )}
              </span>
              <IconButton
                variant="ghost"
                size="sm"
                icon={<Icon icon={RefreshCw} />}
                aria-label="Rotate the token"
                onClick={() => void rotate(r.id)}
              />
              {r.status === "active" ? (
                <Button
                  variant="secondary"
                  size="sm"
                  stretch="auto"
                  label="Disable"
                  onClick={() => void verb(r.id, "disable")}
                />
              ) : (
                <Button
                  variant="secondary"
                  size="sm"
                  stretch="auto"
                  label="Enable"
                  onClick={() => void verb(r.id, "enable")}
                />
              )}
              <IconButton
                variant="ghost"
                size="sm"
                icon={<Icon icon={Trash2} />}
                aria-label="Remove the channel"
                onClick={() => void remove(r.id)}
              />
            </li>
          ))}
        </Rows>
      )}
    </div>
  );
}
