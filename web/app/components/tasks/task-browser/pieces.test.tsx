/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The task browser's row and card open the task at its canonical URL: a Project task
// under its Project, a personal one at /tasks/<id>.
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";
import { emptyCopy, TaskCard, TaskRow } from "./pieces";

const task = (over: Record<string, unknown> = {}) =>
  ({
    id: "t1",
    goal: "Compare three issue trackers",
    title: null,
    status: "running",
    project_id: null,
    created_at: "2026-10-01T10:00:00Z",
    archived_at: null,
    pinned_at: null,
    ...over,
  }) as never;

const render = (el: React.ReactElement) => renderToStaticMarkup(<MemoryRouter>{el}</MemoryRouter>);

describe("the task browser's links", () => {
  it("a list row opens a Project task under its Project", () => {
    expect(render(<TaskRow task={task({ project_id: "s1" })} />)).toContain(
      'href="/projects/s1/tasks/t1"',
    );
  });

  it("a board card opens a Project task under its Project", () => {
    expect(render(<TaskCard task={task({ project_id: "s1" })} />)).toContain(
      'href="/projects/s1/tasks/t1"',
    );
  });

  it("a personal task stays at /tasks/<id> in both", () => {
    expect(render(<TaskRow task={task()} />)).toContain('href="/tasks/t1"');
    expect(render(<TaskCard task={task()} />)).toContain('href="/tasks/t1"');
  });
});

// An empty list says what is true to the reader (fix round 1): "Start something new." is an
// invitation a Project viewer cannot take - the server refuses their write.
describe("the empty list's words", () => {
  it("invites someone who can start a task", () => {
    expect(emptyCopy({ q: "", canStart: true })).toEqual({
      title: "Nothing here yet",
      description: "Start something new.",
    });
  });

  it("tells a viewer what appears here instead", () => {
    const { title, description } = emptyCopy({ q: "", canStart: false });
    expect(title).toBe("Nothing here yet");
    expect(description).toBe("Tasks started in this Project show up here.");
  });

  it("a search with no hits names the search, for everyone", () => {
    for (const canStart of [true, false]) {
      expect(emptyCopy({ q: "pricing", canStart })).toEqual({
        title: "No matches for \u201cpricing\u201d",
        description: undefined,
      });
    }
  });
});
