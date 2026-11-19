/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The sidebar's Projects section - the SIDEBAR SECTION pattern's second speaker:
// a quiet uppercase label, h-9 rows with a leading identity mark
// (the project's face) and a truncating name, active-by-path, hidden on the icon rail,
// capped at a window with the section's own page as the full view. A Project reads as active on
// its page AND everything nested under it (a Project task lives at /projects/<sid>/tasks/<tid>).
// Seeded by the shell loader (best-effort), fresh on every navigation because the shell loader
// re-runs.

import { Link } from "react-router";
import { useNavPath } from "~/hooks/use-pending-path";
import { ProjectIcon } from "~/components/icon-picker";
import {
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
} from "@prismoid/blocks/sidebar";
import type { components } from "~/lib/api/types.gen";
import { isWithin, projectHref } from "~/lib/links";

export type SidebarProjectRow = components["schemas"]["ProjectListOut"];

/** The rail never bloats: five rooms, then one quiet "View all" tail - a LINK to the
 *  section's page, not an in-rail expander (the pattern's law). */
const WINDOW = 5;

export function SidebarProjects({ projects }: { projects: SidebarProjectRow[] }) {
  const pathname = useNavPath();
  if (projects.length === 0) return null;
  const shown = projects.slice(0, WINDOW);
  return (
    <SidebarGroup className="group-data-[collapsible=icon]:hidden">
      <SidebarGroupLabel>Projects</SidebarGroupLabel>
      <SidebarGroupContent>
        <SidebarMenu>
          {shown.map((s) => (
            <SidebarMenuItem key={s.id}>
              <SidebarMenuButton
                isActive={isWithin(pathname, projectHref(s.id))}
                render={<Link to={projectHref(s.id)} />}
              >
                <ProjectIcon icon={s.icon} hue={s.icon_hue} size="sm" />
                <span className="truncate">{s.name}</span>
              </SidebarMenuButton>
            </SidebarMenuItem>
          ))}
          {projects.length > WINDOW && (
            <SidebarMenuItem>
              <SidebarMenuButton render={<Link to="/projects" />} className="text-muted-foreground">
                <span className="truncate">View all projects</span>
              </SidebarMenuButton>
            </SidebarMenuItem>
          )}
        </SidebarMenu>
      </SidebarGroupContent>
    </SidebarGroup>
  );
}
