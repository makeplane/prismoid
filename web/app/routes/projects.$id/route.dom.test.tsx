/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// The project page, driven: the header names the people and shows Invite only to the owner,
// Settings saves a rename, a budget and the charter with the right PATCH body, removes a
// member, adds a project memory, transfers ownership; the Artifacts tab lists and searches.
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ComponentProps } from "react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TaskWindowProvider } from "~/components/tasks/task-window";
import { api } from "~/lib/api/client";
import ProjectDetail from "./route";

type Props = ComponentProps<typeof ProjectDetail>;

const project = (over: Record<string, unknown> = {}) => ({
  id: "p1",
  name: "Atlas",
  role: "owner",
  icon: null,
  icon_hue: null,
  budget_usd: null,
  instructions: "Cite sources.",
  created_at: "2026-11-01T10:00:00Z",
  owner: { user_id: "u-owner", name: "Olive Owner", email: "olive@example.com" },
  ...over,
});
const member = (id: string, name: string, role = "editor") => ({
  actor_id: id,
  name,
  email: `${name.toLowerCase()}@example.com`,
  role,
});

const ok = (data: unknown) => ({
  data,
  error: undefined,
  response: new Response(null, { status: 200 }),
});
const page = (items: unknown[] = []) => ok({ items, next_cursor: null });

let get: ReturnType<typeof vi.spyOn>;
let post: ReturnType<typeof vi.spyOn>;
let patch: ReturnType<typeof vi.spyOn>;
let del: ReturnType<typeof vi.spyOn>;
let memories: unknown[];
let artifacts: unknown[];

beforeEach(() => {
  memories = [];
  artifacts = [];
  get = vi.spyOn(api, "GET").mockImplementation(((path: string) => {
    if (path === "/v1/projects/{project_id}/invites") return Promise.resolve(ok([]));
    if (path === "/v1/tasks/counts") return Promise.resolve(ok({ counts: {} }));
    if (path === "/v1/memories") return Promise.resolve(page(memories));
    if (path === "/v1/projects/{project_id}/artifacts") return Promise.resolve(page(artifacts));
    return Promise.resolve(page());
  }) as never);
  post = vi.spyOn(api, "POST").mockImplementation((() => Promise.resolve(ok({}))) as never);
  patch = vi
    .spyOn(api, "PATCH")
    .mockImplementation(((_p: string, init: { body: Record<string, unknown> }) =>
      Promise.resolve(ok(project(init.body)))) as never);
  del = vi.spyOn(api, "DELETE").mockImplementation((() => Promise.resolve(ok({}))) as never);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  document.body.innerHTML = "";
});

function mount(opts: { proj?: unknown; members?: unknown[]; tab?: string; viewer?: string } = {}) {
  const loaderData = {
    project: opts.proj ?? project(),
    members: opts.members ?? [],
    usage: { month_to_date_usd: "2.50" },
  };
  const router = createMemoryRouter(
    [
      {
        id: "routes/shell/route",
        path: "/",
        loader: () => ({ user: { id: opts.viewer ?? "u-owner" } }),
        children: [
          {
            path: "projects/:id",
            element: (
              <TaskWindowProvider>
                <ProjectDetail {...({ loaderData } as unknown as Props)} />
              </TaskWindowProvider>
            ),
          },
        ],
      },
    ],
    { initialEntries: [`/projects/p1${opts.tab ? `?tab=${opts.tab}` : ""}`] },
  );
  render(<RouterProvider router={router} />);
  return router;
}

const patchBody = () =>
  (patch.mock.calls[patch.mock.calls.length - 1][1] as { body: Record<string, unknown> }).body;

describe("Project header", () => {
  it("names the project, its role and head count, and offers Invite to the owner", async () => {
    mount({ members: [member("a1", "Ada")] });
    expect(await screen.findByRole("heading", { name: "Atlas" })).toBeTruthy();
    expect(screen.getAllByText("owner").length).toBeGreaterThan(0);
    expect(screen.getByText(/2 people/)).toBeTruthy();
    expect(screen.getAllByRole("button", { name: "Invite" }).length).toBeGreaterThan(0);
  });

  it("hides Invite and the owner controls from a viewer", async () => {
    mount({ proj: project({ role: "viewer" }), tab: "settings", viewer: "someone" });
    expect(await screen.findByRole("heading", { name: "Atlas" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Invite" })).toBeNull();
    expect(screen.queryByRole("textbox", { name: "Project name" })).toBeNull();
    expect(screen.getByText("Cite sources.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Change project icon" })).toBeNull();
  });
});

describe("Inviting a member", () => {
  it("posts the trimmed email with the default editor role", async () => {
    const user = userEvent.setup();
    mount();
    const [invite] = await screen.findAllByRole("button", { name: "Invite" });
    await user.click(invite);
    const email = await screen.findByRole("textbox", { name: "Email" });
    const send = screen.getByRole("button", { name: "Send invite" });
    expect((send as HTMLButtonElement).disabled).toBe(true);
    await user.type(email, "  new@example.com ");
    await user.click(send);
    expect(post).toHaveBeenCalledWith("/v1/projects/{project_id}/members", {
      params: { path: { project_id: "p1" } },
      body: { email: "new@example.com", role: "editor" },
    });
    await waitFor(() => expect(screen.queryByRole("textbox", { name: "Email" })).toBeNull());
  });
});

describe("Settings", () => {
  it("saves a rename only once the name changed, sending the clear flags", async () => {
    const user = userEvent.setup();
    mount({ tab: "settings" });
    const name = await screen.findByRole("textbox", { name: "Project name" });
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
    await user.clear(name);
    await user.type(name, "Atlas 2{Enter}");
    expect(patchBody()).toMatchObject({ name: "Atlas 2", clear_budget: false });
  });

  it("sets a budget, and clears it when the field is emptied", async () => {
    const user = userEvent.setup();
    mount({ tab: "settings", proj: project({ budget_usd: "10" }) });
    const budget = await screen.findByRole("textbox", { name: "Budget in USD" });
    await user.clear(budget);
    await user.type(budget, "25{Enter}");
    expect(patchBody()).toMatchObject({ budget_usd: "25", clear_budget: false });
    await user.clear(budget);
    await user.keyboard("{Enter}");
    expect(patchBody()).toMatchObject({ clear_budget: true });
  });

  it("shows the month's spend against the ceiling", async () => {
    mount({ tab: "settings", proj: project({ budget_usd: "10" }) });
    expect(await screen.findByText(/this month/)).toBeTruthy();
  });

  it("saves edited instructions as the trimmed charter", async () => {
    const user = userEvent.setup();
    mount({ tab: "settings" });
    const box = await screen.findByRole("textbox", { name: "Project instructions" });
    await user.type(box, " UK English. ");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(patchBody()).toMatchObject({ instructions: "Cite sources. UK English." });
  });

  it("removes a member by their actor id", async () => {
    const user = userEvent.setup();
    mount({ tab: "settings", members: [member("a1", "Ada")] });
    await user.click(await screen.findByRole("button", { name: "Remove Ada" }));
    expect(del).toHaveBeenCalledWith("/v1/projects/{project_id}/members/{actor_id}", {
      params: { path: { project_id: "p1", actor_id: "a1" } },
    });
  });

  it("shows members read-only to an editor, with no remove button", async () => {
    mount({
      tab: "settings",
      proj: project({ role: "editor" }),
      members: [member("a1", "Ada", "viewer")],
      viewer: "a1",
    });
    expect(await screen.findByText("ada@example.com")).toBeTruthy();
    expect(screen.getByText("Viewer")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Remove Ada" })).toBeNull();
  });

  it("transfers ownership to the typed email", async () => {
    const user = userEvent.setup();
    mount({ tab: "settings" });
    const buttons = await screen.findAllByRole("button", { name: "Transfer ownership" });
    await user.click(buttons[0]);
    await user.type(await screen.findByRole("textbox", { name: "New owner's email" }), "b@x.io");
    const confirm = screen.getAllByRole("button", { name: "Transfer ownership" }).at(-1)!;
    await user.click(confirm);
    expect(post).toHaveBeenCalledWith("/v1/projects/{project_id}/transfer", {
      params: { path: { project_id: "p1" } },
      body: { email: "b@x.io" },
    });
  });
});

describe("Project memory", () => {
  it("lists notes with who wrote them and adds a new one", async () => {
    const user = userEvent.setup();
    memories = [
      {
        id: "m1",
        text: "Prefer tables",
        kind: "note",
        scope: "project",
        created_by_id: "a1",
        created_at: "2026-11-01T10:00:00Z",
        use_count: 2,
      },
    ];
    post.mockResolvedValueOnce(
      ok({
        id: "m2",
        text: "Use UK dates",
        kind: "note",
        scope: "project",
        created_by_id: "u-owner",
        created_at: "2026-11-02T10:00:00Z",
        use_count: 0,
      }) as never,
    );
    mount({ tab: "settings", members: [member("a1", "Ada")] });
    expect(await screen.findByText("Prefer tables")).toBeTruthy();
    expect(screen.getByText(/added by Ada/)).toBeTruthy();
    expect(screen.getByText(/used 2 times/)).toBeTruthy();
    const add = screen.getByRole("textbox", { name: "Add a project memory" });
    await user.type(add, " Use UK dates {Enter}");
    expect(await screen.findByText("Use UK dates")).toBeTruthy();
    expect(post).toHaveBeenCalledWith("/v1/memories", {
      body: { scope: "project", kind: "note", text: "Use UK dates", project_id: "p1" },
    });
    expect((add as HTMLInputElement).value).toBe("");
  });

  it("tells a viewer when nothing is remembered yet", async () => {
    mount({ tab: "settings", proj: project({ role: "viewer" }), viewer: "zed" });
    expect(await screen.findByText("Nothing remembered here yet")).toBeTruthy();
  });
});

describe("Artifacts tab", () => {
  it("lists the project's artifacts and searches by query", async () => {
    const user = userEvent.setup();
    artifacts = [
      {
        id: "x1",
        title: "Market memo",
        type: "document",
        verdict: null,
        created_at: "2026-11-01T10:00:00Z",
      },
    ];
    mount();
    await user.click(await screen.findByRole("tab", { name: "Artifacts" }));
    const row = await screen.findByRole("button", { name: /Market memo/ });
    expect(row).toBeTruthy();
    artifacts = [];
    await user.type(screen.getByPlaceholderText("Search artifacts…"), "zzz");
    expect(await screen.findByText("No matches for “zzz”")).toBeTruthy();
    const last = get.mock.calls.filter((c: unknown[]) => String(c[0]).endsWith("/artifacts"));
    const lastInit = last[last.length - 1][1] as { params: { query: { q: string } } };
    expect(lastInit.params.query.q).toBe("zzz");
  });

  it("says so when the project has published nothing", async () => {
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByRole("tab", { name: "Artifacts" }));
    expect(await screen.findByText("No artifacts yet")).toBeTruthy();
  });
});

describe("Project icon", () => {
  it("lets the owner open the picker", async () => {
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByRole("button", { name: "Change project icon" }));
    const picker = await screen.findByRole("tablist");
    expect(within(picker.parentElement as HTMLElement).getAllByRole("tab").length).toBeGreaterThan(
      0,
    );
  });
});
