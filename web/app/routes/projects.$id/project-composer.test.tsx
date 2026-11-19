/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// A task starts from the Project page's own composer, scoped to that Project.
// Creating is an editor's verb: a viewer who joined by link is offered no write
// the server would refuse - the same rule that hid the old "New task" button.
//
// The composer fetches its own dial rosters (fix round 1) so the Project page paints first; it is
// mounted only for a caller who can start a task, so a viewer fetches nothing.
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";
import type { ComposerOptions } from "~/components/composer/task-composer";
import { canStartTask, ProjectComposer } from "./project-composer";

const options: ComposerOptions = {
  skills: [],
  fleetModels: [],
  connectors: [],
  catalog: null,
  bots: [],
};

const project = (role: string) => ({ id: "s1", name: "Acme", role });

const render = (role: string, opts?: ComposerOptions) =>
  renderToStaticMarkup(
    <MemoryRouter>
      <ProjectComposer project={project(role)} options={opts} />
    </MemoryRouter>,
  );

describe("who may start a task from a Project", () => {
  it("owners and editors, never viewers", () => {
    expect(canStartTask("owner")).toBe(true);
    expect(canStartTask("editor")).toBe(true);
    expect(canStartTask("viewer")).toBe(false);
  });
});

describe("the Project page's composer", () => {
  it("renders for an editor and an owner, naming the Project", () => {
    for (const role of ["editor", "owner"]) {
      const html = render(role, options);
      expect(html).toContain("<textarea");
      expect(html).toContain("Acme");
    }
  });

  it("is scoped: there is no Project picker to wander off with", () => {
    expect(render("editor", options)).not.toContain("where this task will live");
  });

  it("links nowhere through the old home preselect", () => {
    expect(render("editor", options)).not.toContain("?project=");
  });

  it("is absent for a viewer, whether or not options were handed in", () => {
    expect(render("viewer", options)).toBe("");
    expect(render("viewer")).toBe("");
  });

  it("an editor whose rosters are still loading sees a quiet placeholder, never a half-built box", () => {
    const html = render("editor");
    expect(html).not.toContain("<textarea");
    expect(html).toContain('role="status"');
    expect(html).toContain("animate-pulse");
    expect(html).toContain('aria-hidden="true"');
  });
});
