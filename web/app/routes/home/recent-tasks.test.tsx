/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";
import type { TaskWindowRow } from "~/components/tasks/task-window";
import { RecentTaskList } from "./recent-tasks";

const task: TaskWindowRow = {
  id: "62b53b15-c44c-455f-9c78-f752e2bf132e",
  label: "Draft the launch memo",
  status: "running",
  pinned: false,
  createdAt: "2026-07-29T12:00:00Z",
};

describe("RecentTaskList", () => {
  it("keeps an empty account free of recents chrome", () => {
    expect(
      renderToStaticMarkup(
        <MemoryRouter>
          <RecentTaskList tasks={[]} />
        </MemoryRouter>,
      ),
    ).toBe("");
  });

  it("renders semantic links, time, and an accessible status", () => {
    const html = renderToStaticMarkup(
      <MemoryRouter>
        <RecentTaskList tasks={[task]} />
      </MemoryRouter>,
    );
    expect(html).toContain(`href="/tasks/${task.id}"`);
    expect(html).toContain("Draft the launch memo");
    expect(html).toContain('aria-label="Status: running"');
    expect(html).toContain(`dateTime="${task.createdAt}"`);
    expect(html).toContain('href="/tasks"');
  });

  it("a Project task links to its Project's URL, a personal one to /tasks/<id>", () => {
    const html = renderToStaticMarkup(
      <MemoryRouter>
        <RecentTaskList tasks={[{ ...task, id: "t-project", projectId: "s-1" }, task]} />
      </MemoryRouter>,
    );
    expect(html).toContain('href="/projects/s-1/tasks/t-project"');
    expect(html).not.toContain('href="/tasks/t-project"');
    expect(html).toContain(`href="/tasks/${task.id}"`);
  });

  it("offers Hide when the section is toggleable", () => {
    const html = renderToStaticMarkup(
      <MemoryRouter>
        <RecentTaskList tasks={[task]} onToggle={() => {}} />
      </MemoryRouter>,
    );
    expect(html).toContain("Hide");
    expect(html).toContain('aria-expanded="true"');
  });

  it("hidden collapses to one self-recovering line - no rows, no View all", () => {
    const html = renderToStaticMarkup(
      <MemoryRouter>
        <RecentTaskList tasks={[task]} hidden onToggle={() => {}} />
      </MemoryRouter>,
    );
    expect(html).toContain("Show recent");
    expect(html).toContain('aria-expanded="false"');
    expect(html).not.toContain("Draft the launch memo");
    expect(html).not.toContain('href="/tasks"');
  });

  it("an empty account stays pure even when hidden", () => {
    expect(
      renderToStaticMarkup(
        <MemoryRouter>
          <RecentTaskList tasks={[]} hidden onToggle={() => {}} />
        </MemoryRouter>,
      ),
    ).toBe("");
  });
});
