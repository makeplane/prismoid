/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The bot chat's "Details" link opens the task at its canonical URL: a Project bot's
// tasks live in its Project, so the link carries it and skips the redirect hop.
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";
import { DetailsLink } from "./bot-chat";

const render = (el: React.ReactNode) => renderToStaticMarkup(<MemoryRouter>{el}</MemoryRouter>);

describe("DetailsLink", () => {
  it("a Project task opens under its Project", () => {
    expect(render(<DetailsLink task={{ id: "t1", project_id: "s1" }} />)).toContain(
      'href="/projects/s1/tasks/t1"',
    );
  });

  it("a task with no known Project opens at /tasks/<id> (the redirect lands the reader)", () => {
    expect(render(<DetailsLink task={{ id: "t1" }} />)).toContain('href="/tasks/t1"');
  });
});
