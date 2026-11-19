/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The sidebar's Project item reads as active on the Project page AND on every page nested under it
// (a Project task lives at /projects/<sid>/tasks/<tid>) - "you are in Acme" stays true.
import { renderToStaticMarkup } from "react-dom/server";
import { createMemoryRouter, RouterProvider } from "react-router";
import { describe, expect, it } from "vitest";
import { SidebarProvider } from "@prismoid/blocks/sidebar";
import { SidebarProjects, type SidebarProjectRow } from "./sidebar-projects";

const row = (id: string, name: string) =>
  ({ id, name, icon: null, icon_hue: null }) as unknown as SidebarProjectRow;

// A data router, as in the app: the sidebar reads the pending navigation off it.
const render = (at: string) =>
  renderToStaticMarkup(
    <RouterProvider
      router={createMemoryRouter(
        [
          {
            path: "*",
            element: (
              <SidebarProvider>
                <SidebarProjects projects={[row("s1", "Acme"), row("s10", "Beta")]} />
              </SidebarProvider>
            ),
          },
        ],
        { initialEntries: [at] },
      )}
    />,
  );

/** Is the link to this Project marked active? The ATTRIBUTE, not the `data-active:` utility
 *  classes every button carries. */
const active = (html: string, id: string) => {
  const tag = html.match(new RegExp(`<a [^>]*href="/projects/${id}"[^>]*>`))?.[0] ?? "";
  return /\sdata-active(?:="[^"]*")?[\s>]/.test(tag);
};

describe("the sidebar's Project item", () => {
  it("is active on the Project's own page", () => {
    const html = render("/projects/s1");
    expect(active(html, "s1")).toBe(true);
    expect(active(html, "s10")).toBe(false);
  });

  it("is active on a task nested under the Project", () => {
    const html = render("/projects/s1/tasks/t1");
    expect(active(html, "s1")).toBe(true);
  });

  it("never lights a Project whose id merely begins with this one's", () => {
    const html = render("/projects/s10/tasks/t1");
    expect(active(html, "s1")).toBe(false);
    expect(active(html, "s10")).toBe(true);
  });

  it("is quiet on a personal task", () => {
    const html = render("/tasks/t1");
    expect(active(html, "s1")).toBe(false);
  });
});
