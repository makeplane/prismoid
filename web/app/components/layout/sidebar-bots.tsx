/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The sidebar's Bots section (ADR-045) - the rail's list pattern
// applied to pinned bots: leading identity mark (the bot's face), one truncating name, exact-
// path active, a quiet "View all" tail to the messenger. ONLY pinned bots appear - the pin on
// each row of /bots is the membership control, so the rail never bloats by default.

import { Link } from "react-router";
import { useNavPath } from "~/hooks/use-pending-path";
import { BotAvatar } from "~/components/icon-picker";
import {
  SidebarGroup,
  SidebarGroupLabel,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
} from "@prismoid/blocks/sidebar";
import type { components } from "~/lib/api/types.gen";

export type SidebarBotRow = components["schemas"]["BotOut"];

const WINDOW = 8;

export function SidebarBots({ bots }: { bots: SidebarBotRow[] }) {
  const pathname = useNavPath();
  if (bots.length === 0) return null; // nothing pinned ⇒ no section - the rail stays quiet
  const shown = bots.slice(0, WINDOW);
  return (
    <SidebarGroup className="group-data-[collapsible=icon]:hidden">
      <SidebarGroupLabel>Bots</SidebarGroupLabel>
      <SidebarMenu>
        {shown.map((b) => (
          <SidebarMenuItem key={b.name}>
            <SidebarMenuButton
              isActive={pathname === `/bots/${b.name}`}
              render={<Link to={`/bots/${b.name}`} />}
            >
              <BotAvatar name={b.name} icon={b.icon} hue={b.icon_hue} size="sm" />
              <span className="truncate">{b.name}</span>
            </SidebarMenuButton>
          </SidebarMenuItem>
        ))}
        {bots.length > WINDOW && (
          <SidebarMenuItem>
            <SidebarMenuButton className="text-muted-foreground" render={<Link to="/bots" />}>
              <span className="truncate">View all bots…</span>
            </SidebarMenuButton>
          </SidebarMenuItem>
        )}
      </SidebarMenu>
    </SidebarGroup>
  );
}
