/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The modal's ENTRY face (ADR-054): a directory item clicked before it is connected. The
// same header grammar as the connected peek - brand mark, name, vendor, description, the
// honest auth note - with one verb: Connect. Once the connect creates a row, the route
// re-points the modal at it and this face hands over to ConnectorPeek.

import { ExternalLink, Plug } from "lucide-react";
import { ConnectorIcon } from "~/components/connectors/connector-icon";
import type { CatalogEntry } from "~/components/connectors/catalog";
import type { ConnectFlow } from "~/components/connectors/vocabulary";
import { Button } from "@makeplane/propel/components/button";
import { Icon } from "@makeplane/propel/components/icon";

export function OfferFace({
  entry,
  flow,
  onConnect,
}: {
  entry: CatalogEntry;
  flow: ConnectFlow | null;
  onConnect: (slug: string) => void;
}) {
  return (
    <div className="space-y-6 p-6">
      <div className="flex items-start gap-4">
        <ConnectorIcon slug={entry.slug} icon={entry.icon} size="lg" />
        <div className="min-w-0 flex-1">
          <h2 className="font-heading truncate text-h4-regular font-semibold tracking-tight">
            {entry.name}
          </h2>
          <p className="text-muted-foreground mt-0.5 text-caption-md-regular">
            {entry.vendor} · {entry.category}
          </p>
          <p className="text-muted-foreground mt-2 text-body-sm-regular">{entry.description}</p>
          {entry.auth_note && (
            <p className="text-muted-foreground/80 mt-2 text-caption-md-regular">
              {entry.auth_note}
            </p>
          )}
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        {flow ? (
          <>
            {flow.phase === "waiting" && !flow.opened && flow.url && (
              <Button
                nativeButton={false}
                variant="secondary"
                size="sm"
                stretch="auto"
                label="Open authorization page"
                render={<a href={flow.url} target="_blank" rel="noopener noreferrer" />}
              />
            )}
            <span className="text-muted-foreground text-caption-md-regular" aria-live="polite">
              {flow.phase === "starting" ? "Starting…" : "Waiting for the provider…"}
            </span>
          </>
        ) : (
          <Button
            variant="primary"
            size="sm"
            stretch="auto"
            icon={<Icon icon={Plug} />}
            label={entry.auth === "oauth" ? "Connect" : "Add"}
            onClick={() => onConnect(entry.slug)}
          />
        )}
        <span className="flex-1" />
        {entry.docs_url && (
          <Button
            nativeButton={false}
            variant="ghost"
            size="sm"
            stretch="auto"
            icon={<Icon icon={ExternalLink} />}
            label="Docs"
            render={<a href={entry.docs_url} target="_blank" rel="noopener noreferrer" />}
          />
        )}
      </div>
    </div>
  );
}
