/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// One ledger row, shared by every surface that lists memories (Settings → Memory, a project's
// Context section). The caller owns the provenance line (`meta`) and the permission verdict
// (`canMutate`); the row owns edit-in-place and retire - the only two mutations a memory has.
// Both hand their response back (`onEdited`/`onRetired`) so the caller patches its list
// locally instead of refetching it.

import { capitalize } from "~/lib/utils";
import { Pencil, Trash2 } from "lucide-react";
import { memo, useState } from "react";
import { Badge } from "@makeplane/propel/components/badge";
import { Button } from "@makeplane/propel/components/button";
import { Icon } from "@makeplane/propel/components/icon";
import { Input, InputGroup } from "@makeplane/propel/components/input";
import { toastError, api, unwrap } from "~/lib/api/client";
import type { components } from "~/lib/api/types.gen";

export type MemoryRow = components["schemas"]["MemoryOut"];

export const MEMORY_KINDS = ["note", "preference", "fact", "instruction"] as const;
export type MemoryKind = (typeof MEMORY_KINDS)[number];

// Memoized: a search keystroke or a page appended re-renders the list, not every settled row.
// `meta` is a callback (not a node) so the caller can keep it reference-stable across renders.
export const MemoryRowItem = memo(function MemoryRowItem({
  memory,
  meta,
  canMutate,
  onEdited,
  onRetired,
}: {
  memory: MemoryRow;
  /** The caller's provenance line - "from a task · 2d ago", "added by Ana · shared". */
  meta: (memory: MemoryRow) => React.ReactNode;
  canMutate: boolean;
  onEdited: (next: MemoryRow) => void;
  onRetired: (id: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(memory.text);
  const [busy, setBusy] = useState(false);

  async function save() {
    const next = draft.trim();
    if (!next || next === memory.text) {
      setEditing(false);
      return;
    }
    setBusy(true);
    try {
      const updated = await unwrap(
        api.PATCH("/v1/memories/{memory_id}", {
          params: { path: { memory_id: memory.id } },
          body: { text: next },
        }),
      );
      setEditing(false);
      onEdited(updated);
    } catch (e) {
      toastError(e, "Couldn't save the memory.");
    } finally {
      setBusy(false);
    }
  }

  async function retire() {
    try {
      await unwrap(
        api.POST("/v1/memories/{memory_id}/retire", {
          params: { path: { memory_id: memory.id } },
        }),
      );
      onRetired(memory.id);
    } catch (e) {
      toastError(e, "Couldn't retire the memory.");
    }
  }

  return (
    <li className="group flex items-start gap-3 px-4 py-3">
      {/* KIND, not status: a neutral Badge (Pill/StatusBadge are for state/verdict). */}
      <span className="mt-0.5 flex">
        <Badge variant="neutral" size="xs" label={capitalize(memory.kind)} />
      </span>
      <div className="min-w-0 flex-1">
        {editing ? (
          <div className="flex items-center gap-2">
            <InputGroup size="md">
              <Input
                size="md"
                value={draft}
                onChange={(e) => setDraft(e.currentTarget.value)}
                maxLength={500}
                autoFocus
                onKeyDown={(e) => {
                  if (e.key === "Enter") void save();
                  if (e.key === "Escape") setEditing(false);
                }}
              />
            </InputGroup>
            <Button
              variant="primary"
              size="sm"
              stretch="auto"
              label="Save"
              onClick={() => void save()}
              disabled={busy}
            />
          </div>
        ) : (
          <p className="text-body-sm-regular">{memory.text}</p>
        )}
        <p className="text-muted-foreground mt-0.5 text-caption-md-regular">{meta(memory)}</p>
      </div>
      {canMutate && !editing && (
        <span className="flex shrink-0 gap-1 opacity-0 transition-opacity group-focus-within:opacity-100 group-hover:opacity-100">
          <Button
            variant="ghost"
            size="sm"
            stretch="auto"
            icon={<Icon icon={Pencil} />}
            label="Edit"
            onClick={() => {
              setDraft(memory.text);
              setEditing(true);
            }}
          />
          <Button
            variant="danger-outline"
            size="sm"
            stretch="auto"
            icon={<Icon icon={Trash2} />}
            label="Retire"
            onClick={() => void retire()}
          />
        </span>
      )}
    </li>
  );
});
