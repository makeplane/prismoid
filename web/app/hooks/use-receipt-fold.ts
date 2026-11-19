/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The verification receipt's fold on an artifact: closed per artifact, and opened from the
// failed-verification note above the content, which also scrolls the receipt into view.

import { useEffect, useRef, useState } from "react";

export function useReceiptFold(artifactId: string | undefined) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  // Another artifact (or revision) starts with its receipt folded.
  useEffect(() => setOpen(false), [artifactId]);
  function reveal() {
    setOpen(true);
    // After the fold opens: bring the receipt into view (the content above may be long).
    requestAnimationFrame(() =>
      ref.current?.scrollIntoView({ block: "nearest", behavior: "smooth" }),
    );
  }
  return { open, setOpen, ref, reveal };
}
