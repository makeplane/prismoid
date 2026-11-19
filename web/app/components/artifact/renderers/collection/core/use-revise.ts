/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useState } from "react";
import { toast } from "@prismoid/blocks/toast";
import { api, ApiError, toastError, unwrap } from "~/lib/api/client";
import { parseCollection, type Collection } from "~/lib/collection";
import type { ArtifactOut } from "../../registry";
import { buildContent, type Edit } from "./edit-state";

// One save click gets a bounded number of rebase attempts - if the lineage keeps moving under
// us (or something is systematically wrong), we stop, say so, and KEEP the edits staged.
// Unbounded retry was an infinite toast loop when a save came from a non-head revision.
const MAX_REBASES = 3;

/**
 * The save half of direct manipulation: staged commands → the lineage's next
 * revision. A 409 (the lineage head moved, or the viewer is on an OLD revision via the history
 * chips) REBASES: the same commands re-applied over the newest content, cell-level
 * last-write-wins - staged work is never lost and nothing is silently clobbered. The retry
 * must target the lineage HEAD's id: the server (operations.revise) accepts a revise only on
 * the newest revision, so re-posting to whichever revision this viewer happens to have open
 * would 409 forever. A failed save keeps the commands staged.
 */
export function useRevise(
  artifact: ArtifactOut,
  base: Collection,
  onSaved: (newArtifactId: string) => void,
) {
  const [saving, setSaving] = useState(false);

  const save = async (
    edits: Edit[],
    onto: Collection = base,
    baseSha: string = artifact.sha256,
    targetId: string = artifact.id,
    attempt = 0,
  ) => {
    setSaving(true);
    try {
      const revised = await unwrap(
        api.POST("/v1/artifacts/{artifact_id}/revise", {
          params: { path: { artifact_id: targetId } },
          body: { content: buildContent(onto, edits), base_sha256: baseSha },
        }),
      );
      toast.success(`Saved as v${revised.revision}`);
      onSaved(revised.id);
    } catch (e) {
      // Status, not message-sniffing (the text-editor hook's shape): rewording the server's
      // Conflict must never silently break only this editor's rebase. Every revise 409 on a
      // collection IS the lineage-moved case.
      if (e instanceof ApiError && e.status === 409 && attempt < MAX_REBASES) {
        await rebase(edits, attempt + 1);
      } else {
        toastError(e, "Couldn't save the revision. Your edits are still staged");
      }
    } finally {
      setSaving(false);
    }
  };

  const rebase = async (edits: Edit[], attempt: number) => {
    try {
      // The revisions endpoint resolves the LINEAGE from any member id, newest first.
      const page = await unwrap(
        api.GET("/v1/artifacts/{artifact_id}/revisions", {
          params: { path: { artifact_id: artifact.id } },
        }),
      );
      const newest = page.items[0];
      const download = await unwrap(
        api.GET("/v1/artifacts/{artifact_id}/download", {
          params: { path: { artifact_id: newest.id } },
        }),
      );
      const text = await fetch(download.url).then((r) => r.text());
      const parsed = parseCollection(text);
      if (!parsed.ok) throw new Error(parsed.reason);
      toast.info(`Rebasing your ${edits.length} change(s) onto v${newest.revision}…`);
      // THE fix: retry against the HEAD's id + sha, not the revision this viewer opened.
      await save(edits, parsed.data, newest.sha256, newest.id, attempt);
    } catch (e) {
      toastError(e, "Couldn't rebase. Your edits are still staged");
    }
  };

  return { save, saving };
}
