/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// A forked task's lineage, loaded once per parent. The loader already knows the parent
// (TaskDetailOut.parent_task_id), so the chain loads at mount - no waiting for the SSE
// `task.created` frame to reveal it; the stream's parent stays the fallback for legacy rows.

import { useEffect, useRef, useState } from "react";
import { loadAncestors, type Ancestor } from "./thread";

export function useAncestors(
  loaderParent: string | null | undefined,
  streamParent: string | null | undefined,
): { ancestors: Ancestor[]; ancestorsError: boolean } {
  const [ancestors, setAncestors] = useState<Ancestor[]>([]);
  const [ancestorsError, setAncestorsError] = useState(false);
  const loadedParent = useRef<string | null>(null);
  useEffect(() => {
    const parent = loaderParent ?? streamParent;
    if (parent && loadedParent.current !== parent) {
      loadedParent.current = parent;
      loadAncestors(parent)
        .then(setAncestors)
        .catch(() => setAncestorsError(true)); // surfaced in the transcript - never silently missing
    }
  }, [loaderParent, streamParent]);
  return { ancestors, ancestorsError };
}
