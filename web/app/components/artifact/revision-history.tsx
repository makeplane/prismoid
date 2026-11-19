/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The revision history (ADR-033): the lineage as a story - who made each
// version, when, what it was named, and the levers: view any, restore any (by MINTING a new
// head, never overwriting), name any. Lives INSIDE ArtifactView so all four hosts (full
// screen, split pane, Sheet drawer, ds) carry the same surface; collapsed by default - the
// content is the page, the history is a receipt you can open.
import { Badge } from "@makeplane/propel/components/badge";
import { Button } from "@makeplane/propel/components/button";
import { Icon } from "@makeplane/propel/components/icon";
import { IconButton } from "@makeplane/propel/components/icon-button";
import { Input, InputGroup } from "@makeplane/propel/components/input";
import { Download, History, Pencil } from "lucide-react";
import { useState } from "react";
import { toast } from "@prismoid/blocks/toast";
import { QuietDisclosure, VerdictBadge } from "~/components/kit";
import {
  Popover,
  PopoverContent,
  PopoverDescription,
  PopoverTitle,
  PopoverTrigger,
} from "@makeplane/propel/components/popover";
import { api, toastError, unwrap } from "~/lib/api/client";
import { ago } from "~/lib/format";
import { cn } from "~/lib/utils";
import type { components } from "~/lib/api/types.gen";

type ArtifactOut = components["schemas"]["ArtifactOut"];

/** The row's derived story - pure, so the law is unit-testable without a DOM: who made it
 *  ("you" for user provenance), and whether it is a RESTORE (its bytes equal an EARLIER
 *  revision's - derivable, never a schema field). */
export function revisionMeta(
  revisions: ArtifactOut[],
  r: ArtifactOut,
): { who: string; restoredFrom: number | null } {
  const maker = r.provenance["agent"];
  const who = maker === "user" ? "you" : maker ? String(maker) : "the system";
  // Oldest match wins: after repeated restores of v1, every copy reads "restored from v1"
  // (the origin), never from a sibling restore that happens to carry the same bytes.
  const earlier = [...revisions]
    .reverse()
    .find((candidate) => candidate.revision < r.revision && candidate.sha256 === r.sha256);
  return { who, restoredFrom: earlier ? earlier.revision : null };
}

/** What a history row offers: naming a revision and restoring one are writes the server allows
 *  the task's creator and its Project's editors only (`artifacts._require_edit_rights`), so a
 *  read-only reader (a Project viewer) gets neither; the head is never restored onto itself.
 *  Download is everyone's and is not asked here. Pure. */
export function revisionActions(
  r: Pick<ArtifactOut, "id">,
  head: Pick<ArtifactOut, "id">,
  readOnly: boolean,
): { label: boolean; restore: boolean } {
  return { label: !readOnly, restore: !readOnly && r.id !== head.id };
}

export function RevisionHistory({
  revisions,
  viewedId,
  onView,
  onAdvance,
  readOnly = false,
}: {
  /** The lineage, newest first - ArtifactView's one fetch owns it. */
  revisions: ArtifactOut[];
  viewedId: string;
  /** Jump the viewer to a revision (no new head - just looking). */
  onView: (id: string) => void;
  /** A restore minted a NEW head - the viewer and the host must follow it. */
  onAdvance: (artifact: ArtifactOut) => void;
  /** The reader may not write here (a Project viewer): no label, no restore. */
  readOnly?: boolean;
}) {
  const [busy, setBusy] = useState(false);
  // Optimistic label overrides by row id; the canonical value self-corrects on the next
  // quartet fetch (a view/advance re-runs it), so a failed save can simply roll back.
  const [labels, setLabels] = useState<Record<string, string | null>>({});

  if (revisions.length < 2) return null;
  const head = revisions[0];
  const viewed = revisions.find((r) => r.id === viewedId);

  async function restore(target: ArtifactOut) {
    setBusy(true);
    try {
      const minted = await unwrap(
        api.POST("/v1/artifacts/{artifact_id}/restore", {
          params: { path: { artifact_id: target.id } },
          body: { base_sha256: head.sha256 },
        }),
      );
      toast(`Restored v${target.revision} as v${minted.revision}`, {
        description: "Nothing was overwritten. The whole history is still here.",
      });
      onAdvance(minted);
    } catch (e) {
      toastError(e, "Couldn't restore. The lineage may have moved; reopen History and retry.");
    } finally {
      setBusy(false);
    }
  }

  async function saveLabel(target: ArtifactOut, raw: string) {
    const label = raw.trim() ? raw.trim() : null;
    // `in`, not `??`: a legit optimistic value IS null (a clear) - `??` would roll a failed
    // later save back to the stale server label instead of the cleared state.
    const prior = target.id in labels ? labels[target.id] : (target.label ?? null);
    setLabels((m) => ({ ...m, [target.id]: label }));
    try {
      await unwrap(
        api.POST("/v1/artifacts/{artifact_id}/label", {
          params: { path: { artifact_id: target.id } },
          body: { label },
        }),
      );
    } catch (e) {
      setLabels((m) => ({ ...m, [target.id]: prior }));
      toastError(e, "Couldn't save the label.");
    }
  }

  async function downloadRevision(target: ArtifactOut) {
    try {
      const d = await unwrap(
        api.GET("/v1/artifacts/{artifact_id}/download", {
          params: { path: { artifact_id: target.id } },
        }),
      );
      const a = document.createElement("a");
      a.href = d.url;
      a.download = d.filename;
      a.click();
    } catch (e) {
      toastError(e, "Couldn't fetch that version's download link.");
    }
  }

  return (
    <QuietDisclosure
      label={
        <span className="inline-flex items-center gap-1">
          <History className="size-3" /> History
          {viewed && viewed.id !== head.id && (
            <span className="text-muted-foreground/70">· viewing v{viewed.revision}</span>
          )}
        </span>
      }
      count={revisions.length}
    >
      <ul className="mt-2 space-y-0.5">
        {revisions.map((r) => {
          const label = labels[r.id] !== undefined ? labels[r.id] : r.label;
          const { who, restoredFrom } = revisionMeta(revisions, r);
          const can = revisionActions(r, head, readOnly);
          return (
            <li key={r.id}>
              <div
                className={cn(
                  "group/rev hover:bg-muted/40 flex items-center gap-2.5 rounded-lg px-2 py-1.5 transition-colors",
                  r.id === viewedId && "bg-muted/60",
                )}
              >
                <button
                  type="button"
                  onClick={() => onView(r.id)}
                  className="flex min-w-0 flex-1 items-center gap-2.5 text-left"
                >
                  <span className="shrink-0 tabular-nums">
                    <Badge variant="neutral" size="xs" label={`v${r.revision}`} />
                  </span>
                  <span className="min-w-0 flex-1 truncate text-caption-md-regular">
                    {label && <span className="text-foreground font-medium">{label} · </span>}
                    <span className="text-muted-foreground">
                      {who}
                      {restoredFrom != null ? ` · restored from v${restoredFrom}` : ""}
                      {r.id === head.id ? " · latest" : ""} · {ago(r.created_at)}
                    </span>
                  </span>
                  <VerdictBadge verdict={r.verdict} />
                </button>
                <span className="flex shrink-0 items-center gap-0.5 opacity-0 transition-opacity group-hover/rev:opacity-100 focus-within:opacity-100">
                  {can.label && (
                    <LabelEditor
                      revision={r.revision}
                      label={label ?? null}
                      onSave={(raw) => void saveLabel(r, raw)}
                    />
                  )}
                  <IconButton
                    size="sm"
                    variant="ghost"
                    icon={<Icon icon={Download} />}
                    aria-label={`Download v${r.revision}`}
                    onClick={() => void downloadRevision(r)}
                  />
                  {can.restore && (
                    <Popover>
                      <PopoverTrigger
                        render={
                          <IconButton
                            size="sm"
                            variant="ghost"
                            icon={<Icon icon={History} />}
                            aria-label={`Restore v${r.revision}`}
                          />
                        }
                      />
                      <PopoverContent variant="rich" align="end">
                        {/* w-64: the rich panel is content-width, so the sentence needs a measure. */}
                        <div className="w-64 space-y-2.5">
                          <PopoverTitle>
                            Restore v{r.revision} as v{head.revision + 1}?
                          </PopoverTitle>
                          <PopoverDescription>
                            The current head stays in history - restoring mints a new version
                            carrying v{r.revision}'s content.
                          </PopoverDescription>
                          <Button
                            variant="primary"
                            size="sm"
                            stretch="auto"
                            label="Restore"
                            disabled={busy}
                            onClick={() => void restore(r)}
                          />
                        </div>
                      </PopoverContent>
                    </Popover>
                  )}
                </span>
              </div>
            </li>
          );
        })}
      </ul>
    </QuietDisclosure>
  );
}

/** The per-row name popover - CONTROLLED so Enter actually finishes: the save is optimistic
 *  (the row's label updates behind the popup), and an open popup after submit read as "did
 *  that work?". Closing on submit is the confirmation. */
function LabelEditor({
  revision,
  label,
  onSave,
}: {
  revision: number;
  label: string | null;
  onSave: (raw: string) => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger
        render={
          <IconButton
            size="sm"
            variant="ghost"
            icon={<Icon icon={Pencil} />}
            aria-label={`Label v${revision}`}
          />
        }
      />
      <PopoverContent variant="rich" align="end">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            onSave(String(new FormData(e.currentTarget).get("label") ?? ""));
            setOpen(false);
          }}
          className="space-y-2"
        >
          <PopoverTitle>Name v{revision}</PopoverTitle>
          <InputGroup size="md">
            <Input
              size="md"
              name="label"
              defaultValue={label ?? ""}
              placeholder="final draft"
              maxLength={80}
              autoFocus
            />
          </InputGroup>
          <PopoverDescription>Enter saves · empty clears</PopoverDescription>
        </form>
      </PopoverContent>
    </Popover>
  );
}
