/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { Component, type ReactNode } from "react";

/**
 * The renderer subtree's crash containment - the app's first component-level boundary, scoped
 * this narrowly on purpose: a bug in one kind's renderer (or a stale lazy chunk after a deploy)
 * degrades to the fallback card, never a white drawer or a dead task page.
 */
export class RendererBoundary extends Component<
  { fallback: ReactNode; children: ReactNode },
  { failed: boolean }
> {
  override state = { failed: false };

  static getDerivedStateFromError(): { failed: boolean } {
    return { failed: true };
  }

  override render(): ReactNode {
    return this.state.failed ? this.props.fallback : this.props.children;
  }
}
