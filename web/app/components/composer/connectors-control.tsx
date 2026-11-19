/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The Connectors control (ADR-020) - a named pill that opens per-connector switches with
// Add/Manage beneath. On the task-minting box the Options menu's Connectors row is the door
// that is ALWAYS there (how a person discovers connectors exist: every state has a real action,
// toggle when live, add or manage otherwise); this pill joins the row once a connector is
// attached, so what the task may reach is visible at a glance. Other composers mount it directly.
//
// Controlled like every dial: the host owns the catalog and the selected ids; toggling here
// chooses what THIS task may reach - nothing attaches implicitly.

import { Plug, Plus, Settings2 } from "lucide-react";
import { Link } from "react-router";
import { Button } from "@makeplane/propel/components/button";
import { Button as ButtonElement } from "@makeplane/propel/elements/button";
import { Icon } from "@makeplane/propel/components/icon";
import { Popover, PopoverContent, PopoverTrigger } from "@makeplane/propel/components/popover";
import { Switch } from "@makeplane/propel/components/switch";
import type { components } from "~/lib/api/types.gen";

export type ConnectorOut = components["schemas"]["ConnectorOut"];

/** What the switches may offer: only a live connector can be attached (the door 422s the
 *  rest, so a toggle on anything else would be a promise the send cannot keep). */
export const attachable = (connectors: ConnectorOut[]) =>
  connectors.filter((c) => c.status === "live");

export function ConnectorsControl({
  connectors,
  ids,
  onChange,
}: {
  connectors: ConnectorOut[];
  ids: string[];
  onChange: (ids: string[]) => void;
}) {
  const live = attachable(connectors);
  const parked = connectors.length - live.length;

  function toggle(id: string, on: boolean) {
    onChange(on ? [...ids, id] : ids.filter((x) => x !== id));
  }

  return (
    <Popover>
      <PopoverTrigger
        render={
          // The composer bar's ghost look (the dials' SelectTriggers); a set state fills
          // (tertiary) - attached credentials must be visible at a glance, not two clicks deep.
          <ButtonElement
            variant={ids.length > 0 ? "tertiary" : "ghost"}
            size="md"
            stretch="auto"
            aria-label={
              // The fill-at-a-glance promise, audible too: the count rides the accessible name.
              ids.length > 0 ? `Connectors · ${ids.length} attached` : "Connectors"
            }
            title="Connectors: external services this task may call"
          >
            <Icon icon={Plug} />
            {ids.length > 0 && <span className="tabular-nums">{ids.length}</span>}
          </ButtonElement>
        }
      />
      <PopoverContent align="start">
        {/* The rich panel sizes to its content (w-max): the fixed column keeps copy wrapping. */}
        <div className="w-72 space-y-1">
          {live.length > 0 ? (
            <>
              <p className="text-muted-foreground px-2 pt-1 pb-1.5 text-caption-md-regular">
                External services this task may call directly.
              </p>
              {live.map((c) => {
                const on = ids.includes(c.id);
                return (
                  <label
                    key={c.id}
                    className="hover:bg-accent/40 flex min-h-8 cursor-pointer items-center gap-2.5 rounded-md px-2 py-1 transition-colors"
                    title={`${c.server_url} · ${c.tools.length} tool${c.tools.length === 1 ? "" : "s"}`}
                  >
                    <Plug className="text-muted-foreground size-3.5 shrink-0" />
                    <span className="min-w-0 flex-1 truncate text-body-sm-regular">{c.name}</span>
                    <Switch
                      size="md"
                      checked={on}
                      onCheckedChange={(next) => toggle(c.id, next === true)}
                      aria-label={`Use ${c.name} on this task`}
                    />
                  </label>
                );
              })}
            </>
          ) : (
            <p className="text-muted-foreground px-2 pt-1 pb-1.5 text-caption-md-regular">
              Connect an external service - Notion, Linear, your own MCP server - and its tools
              become callable from tasks.
            </p>
          )}
          <div className="bg-border/60 mx-1 my-1.5 h-px" role="separator" />
          {/* Both doors lead to /connectors now: the catalog, the custom-URL
              dialog, and the per-tool permissions all live on the one page. */}
          <Button
            nativeButton={false}
            variant="ghost"
            size="sm"
            stretch="full"
            icon={<Icon icon={Plus} />}
            label="Add connector…"
            render={<Link to="/connectors" />}
          />
          {connectors.length > 0 && (
            // The element, not the ready-made: the row also carries the to-authorize count.
            <ButtonElement
              variant="ghost"
              size="sm"
              stretch="full"
              render={<Link to="/connectors" />}
            >
              <Icon icon={Settings2} /> Manage connectors
              {parked > 0 && (
                <span className="text-muted-foreground/70 ml-auto text-caption-md-regular">
                  {parked} to authorize
                </span>
              )}
            </ButtonElement>
          )}
        </div>
      </PopoverContent>
    </Popover>
  );
}
