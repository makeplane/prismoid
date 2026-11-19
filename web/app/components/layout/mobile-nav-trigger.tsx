/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { SidebarTrigger, useSidebar } from "@prismoid/blocks/sidebar";

/** The phone's way into the nav. Below the mobile breakpoint the sidebar is an off-canvas sheet,
 *  and its own collapse control lives inside that sheet - so the shell header had no way to open
 *  it at all. Wide screens keep the control in the sidebar's header and render nothing here. */
export function MobileNavTrigger() {
  const { isMobile } = useSidebar();
  return isMobile ? <SidebarTrigger /> : null;
}
