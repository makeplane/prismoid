/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The composer's quiet offer (ADR-054): "Use Gmail", beside the connectors dial - appears
// when the draft's words match a service, attaches on tap (live) or runs the one-click
// connect first (catalog-only). Never steals focus, dismissible per draft, ABSENT when
// nothing matches (the capability law: an inapplicable option does not exist).

import { Button } from "@makeplane/propel/components/button";
import { Icon } from "@makeplane/propel/components/icon";
import { IconButton } from "@makeplane/propel/components/icon-button";
import { Plug, X } from "lucide-react";
import type { ConnectFlow } from "./vocabulary";
import type { ConnectorSuggestion } from "./suggest";

export function ConnectorSuggestionChip({
  suggestion,
  flow,
  onAttach,
  onConnect,
  onDismiss,
}: {
  suggestion: ConnectorSuggestion;
  /** The in-flight one-click connect for this slug, when one is running. */
  flow: ConnectFlow | null;
  onAttach: (connectorId: string) => void;
  onConnect: (slug: string) => void;
  onDismiss: (slug: string) => void;
}) {
  const live = suggestion.connectorId !== null;
  return (
    <span className="flex shrink-0 items-center" data-testid="connector-suggestion">
      <Button
        type="button"
        variant="ghost"
        size="md"
        stretch="auto"
        icon={<Icon icon={Plug} />}
        label={
          flow
            ? "Connecting…"
            : live
              ? `Use ${suggestion.name}`
              : `Use ${suggestion.name} · connect`
        }
        disabled={flow !== null}
        title={
          live
            ? `Attach ${suggestion.name} to this task`
            : `Connect ${suggestion.name}, then attach it`
        }
        onClick={() =>
          live && suggestion.connectorId
            ? onAttach(suggestion.connectorId)
            : onConnect(suggestion.slug)
        }
      />
      <IconButton
        type="button"
        variant="ghost"
        size="sm"
        icon={<Icon icon={X} />}
        aria-label={`Dismiss the ${suggestion.name} suggestion`}
        onClick={() => onDismiss(suggestion.slug)}
      />
    </span>
  );
}
