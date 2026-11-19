/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// One connector card - a connected row (status pill, opens the peek) or a catalog offer
// (Connect button running the one-click flow INSIDE the click gesture). Route-local: composes
// kit + ui + the connectors vocabulary, invents nothing visual.

import { Button } from "@makeplane/propel/components/button";
import { Card, CardContent } from "@prismoid/blocks/card";
import { Pill } from "~/components/kit";
import { ConnectorIcon } from "~/components/connectors/connector-icon";
import type { ConnectorCard } from "~/components/connectors/catalog";
import {
  CONNECTOR_TONE,
  type ConnectFlow,
  connectorStatusLabel,
} from "~/components/connectors/vocabulary";

export function ConnectorCardFace({
  card,
  flow,
  onOpen,
  onConnect,
}: {
  card: ConnectorCard;
  /** The in-flight one-click flow for this card (keyed by slug pre-create, id after). */
  flow: ConnectFlow | null;
  onOpen: () => void;
  onConnect: (slug: string) => void;
}) {
  if (card.kind === "mine") {
    const { row, entry } = card;
    return (
      <Card
        size="sm"
        className="hover:ring-foreground/25 cursor-pointer transition-shadow"
        onClick={onOpen}
        role="button"
        aria-label={`${row.name}: ${connectorStatusLabel(row.status)}`}
      >
        <CardContent>
          <div className="flex items-start gap-3">
            <ConnectorIcon slug={row.catalog_slug ?? row.slug} icon={entry?.icon} />
            <div className="min-w-0 flex-1">
              <div className="flex items-center justify-between gap-2">
                <span className="truncate text-body-sm-regular font-medium">{row.name}</span>
                <Pill tone={CONNECTOR_TONE[row.status]}>{connectorStatusLabel(row.status)}</Pill>
              </div>
              <p className="text-muted-foreground mt-1 line-clamp-2 text-caption-md-regular">
                {entry?.description ?? row.server_url}
              </p>
            </div>
          </div>
        </CardContent>
      </Card>
    );
  }

  const { entry } = card;
  return (
    <Card
      size="sm"
      className="hover:ring-foreground/25 cursor-pointer transition-shadow"
      onClick={onOpen}
      role="button"
      aria-label={`${entry.name}: available`}
    >
      <CardContent>
        <div className="flex items-start gap-3">
          <ConnectorIcon slug={entry.slug} icon={entry.icon} />
          <div className="min-w-0 flex-1">
            <div className="flex items-center justify-between gap-2">
              <span className="truncate text-body-sm-regular font-medium">{entry.name}</span>
              <Button
                variant="secondary"
                size="sm"
                stretch="auto"
                disabled={flow !== null}
                onClick={(e) => {
                  e.stopPropagation(); // the card opens the modal; the button connects
                  onConnect(entry.slug);
                }}
                label={flow ? "Connecting…" : entry.auth === "oauth" ? "Connect" : "Add"}
              />
            </div>
            <p className="text-muted-foreground mt-1 line-clamp-2 text-caption-md-regular">
              {entry.description}
            </p>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
