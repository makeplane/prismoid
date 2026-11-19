/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The text save path (ADR-033) - deliberately SIMPLER than the collection editor's
// use-revise: text has no command log to re-apply, so there is no auto-rebase. A 409 keeps
// the draft staged, re-bases the NEXT save on the newer head, and says so; the person is
// the merge engine, with their words intact.
import { useEffect, useState } from "react";
import { toast } from "@prismoid/blocks/toast";
import { api, ApiError, toastError, unwrap } from "~/lib/api/client";
import type { components } from "~/lib/api/types.gen";

type ArtifactOut = components["schemas"]["ArtifactOut"];

export function useReviseText(artifact: ArtifactOut, onSaved: (artifactId: string) => void) {
  const [saving, setSaving] = useState(false);
  // The save target: seeded from the viewed artifact, replaced by the lineage head after a
  // conflict so the next save lands on the newer base.
  const [base, setBase] = useState({ id: artifact.id, sha256: artifact.sha256 });
  // The newer revision a conflict surfaced - the banner's number; null = no conflict.
  const [conflict, setConflict] = useState<number | null>(null);
  useEffect(() => {
    setBase({ id: artifact.id, sha256: artifact.sha256 });
    setConflict(null);
  }, [artifact.id, artifact.sha256]);

  async function save(text: string): Promise<boolean> {
    setSaving(true);
    try {
      const revised = await unwrap(
        api.POST("/v1/artifacts/{artifact_id}/revise", {
          params: { path: { artifact_id: base.id } },
          body: { content: text, base_sha256: base.sha256 },
        }),
      );
      toast(`Saved as v${revised.revision}`);
      onSaved(revised.id);
      return true;
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        try {
          const page = await unwrap(
            api.GET("/v1/artifacts/{artifact_id}/revisions", {
              params: { path: { artifact_id: base.id } },
            }),
          );
          const head = page.items[0];
          if (head) {
            setBase({ id: head.id, sha256: head.sha256 });
            setConflict(head.revision);
            return false; // the draft is KEPT; the banner explains; Save again lands on the head
          }
        } catch {
          // fall through to the generic toast - the draft is still staged
        }
      }
      toastError(e, "Couldn't save. Your edits are still staged.");
      return false;
    } finally {
      setSaving(false);
    }
  }

  return { save, saving, conflict };
}
