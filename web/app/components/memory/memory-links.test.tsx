/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// A memory's provenance link opens its source task at the canonical URL when the row names a
// Project (fix round 1) - so most clicks skip the redirect hop - and at /tasks/<id>
// when it does not (the redirect lands the reader).
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";
import { userMemoryMeta } from "~/components/layout/settings-panels";
import { botMemoryMeta } from "~/routes/bots/memory-tab";

const row = (over: Record<string, unknown> = {}) =>
  ({
    id: "m1",
    source_task_id: "t1",
    project_id: null,
    created_at: "2026-10-01T10:00:00Z",
    use_count: 0,
    ...over,
  }) as never;

const render = (el: React.ReactNode) => renderToStaticMarkup(<MemoryRouter>{el}</MemoryRouter>);

describe("memory provenance links", () => {
  it("a bot memory from a Project task links under its Project", () => {
    expect(render(botMemoryMeta(row({ project_id: "s1" })))).toContain(
      'href="/projects/s1/tasks/t1"',
    );
  });

  it("a user memory from a personal task links /tasks/<id>", () => {
    expect(render(userMemoryMeta(row()))).toContain('href="/tasks/t1"');
  });

  it("a user memory row that names a Project uses it too", () => {
    expect(render(userMemoryMeta(row({ project_id: "s1" })))).toContain(
      'href="/projects/s1/tasks/t1"',
    );
  });

  it("a memory added by hand links nothing", () => {
    expect(render(botMemoryMeta(row({ source_task_id: null })))).not.toContain("<a ");
  });
});
