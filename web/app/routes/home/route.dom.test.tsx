/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// Home: New task landing on a Home already open puts the cursor back in the composer, the way
// the first arrival does.
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, Link, RouterProvider } from "react-router";
import { afterEach, describe, expect, it } from "vitest";
import { TaskWindowProvider } from "~/components/tasks/task-window";
import { NEW_TASK_HREF } from "~/lib/links";
import NewTask from "./route";

afterEach(() => {
  cleanup();
  document.body.innerHTML = "";
});

const loaderData = {
  options: { skills: [], fleetModels: [], connectors: [], catalog: null, bots: [] },
  initialProject: null,
};

function mount() {
  const page = (
    <TaskWindowProvider initial={{ pins: [], recents: [] }}>
      <Link to={NEW_TASK_HREF}>New task</Link>
      <NewTask {...({ loaderData } as unknown as Parameters<typeof NewTask>[0])} />
    </TaskWindowProvider>
  );
  const router = createMemoryRouter([{ path: "/", element: page }], { initialEntries: ["/"] });
  render(<RouterProvider router={router} />);
  return router;
}

describe("Home composer focus", () => {
  it("New task on an open Home refocuses the box", async () => {
    const user = userEvent.setup();
    mount();
    const box = screen.getByRole("textbox");
    await waitFor(() => expect(document.activeElement).toBe(box));
    await user.click(screen.getByRole("link", { name: "New task" }));
    await waitFor(() => expect(document.activeElement).toBe(box));
  });
});
