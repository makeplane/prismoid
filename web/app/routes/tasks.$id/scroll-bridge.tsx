/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { type MutableRefObject, useEffect } from "react";
import { useMessageScroller } from "@prismoid/blocks/message-scroller";

/** The composer's hand on the scroller. `useMessageScroller` only works under the provider,
 *  and `submit()` lives above it - this bridge runs inside and hands the two moves out through
 *  a ref: pin a fresh turn's bubble to the TOP of the viewport (ChatGPT's grammar - your
 *  prompt anchors, the answer grows below it), or glide to the tail where a steer's bubble is
 *  about to land. Before this, sending a prompt left the viewport wherever it was and the
 *  user scrolled by hand to find their own words. */
export type ScrollApi = { toTurn: (id: string) => void; toEnd: () => void };

export function ScrollBridge({ apiRef }: { apiRef: MutableRefObject<ScrollApi | null> }) {
  const { scrollToMessage, scrollToEnd } = useMessageScroller();
  useEffect(() => {
    apiRef.current = {
      toTurn: (id) => void scrollToMessage(id, { align: "start", behavior: "smooth" }),
      toEnd: () => void scrollToEnd({ behavior: "smooth" }),
    };
    return () => {
      apiRef.current = null;
    };
  }, [apiRef, scrollToMessage, scrollToEnd]);
  return null;
}
