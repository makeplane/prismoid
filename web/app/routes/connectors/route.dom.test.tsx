/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// The connectors page and its detail modal: the catalog and your rows merge into cards, the
// lens chips and search narrow them, a card opens the modal (URL-borne), and the modal's verbs
// (permissions, disable, retry, remove, one-click connect) each hit the right wire path.
import { toast } from "@prismoid/blocks/toast";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ComponentProps } from "react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "~/lib/api/client";
import { TOPBAR_SLOT_ID } from "~/components/layout/topbar-slot";
import Connectors from "./route";

type Props = ComponentProps<typeof Connectors>;
type Row = Props["loaderData"]["rows"][number];
type Entry = NonNullable<Props["loaderData"]["catalog"]>[number];

const entry = (slug: string, over: Partial<Entry> = {}): Entry => ({
  auth: "oauth",
  auth_note: "",
  category: "Work",
  description: `${slug} description`,
  docs_url: "",
  icon: "",
  keywords: [],
  name: slug.toUpperCase(),
  server_url: `https://${slug}.example/mcp`,
  slug,
  vendor: "Acme",
  ...over,
});

const tool = (name: string, over: Record<string, unknown> = {}) => ({
  name,
  description: `${name} tool`,
  policy: "ask",
  policy_source: "default",
  read_only: false,
  destructive: false,
  ...over,
});

const row = (over: Partial<Row> = {}): Row =>
  ({
    auth_kind: "oauth",
    auto_attach: false,
    catalog_slug: "alpha",
    created_at: "2026-11-01T10:00:00Z",
    id: "c1",
    instructions: null,
    last_error: null,
    name: "Alpha Mine",
    server_url: "https://alpha.example/mcp",
    slug: "alpha",
    status: "live",
    suggest_words: [],
    tools: [tool("read_doc", { read_only: true, policy: "allow" }), tool("write_doc")],
    ...over,
  }) as Row;

const ok = (data: unknown) =>
  Promise.resolve({ data, error: undefined, response: new Response(null, { status: 200 }) });
const catalog = [entry("alpha"), entry("beta", { category: "Dev", auth: "open" })];

let post: ReturnType<typeof vi.spyOn>;
let patch: ReturnType<typeof vi.spyOn>;
let del: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  post = vi.spyOn(api, "POST").mockImplementation((() => ok(row())) as never);
  patch = vi.spyOn(api, "PATCH").mockImplementation((() => ok(row())) as never);
  del = vi.spyOn(api, "DELETE").mockImplementation((() => ok({})) as never);
  vi.spyOn(api, "GET").mockImplementation((() => ok({ items: [], next_cursor: null })) as never);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  localStorage.clear();
  document.body.innerHTML = "";
});

function mount(rows: Row[], url = "/connectors") {
  // The topbar's end slot is where search, the funnel and Add portal to, as in the shell.
  const slot = document.createElement("div");
  slot.id = TOPBAR_SLOT_ID;
  document.body.append(slot);
  const router = createMemoryRouter(
    [
      {
        path: "/connectors",
        element: <Connectors {...({ loaderData: { rows, catalog } } as unknown as Props)} />,
      },
    ],
    { initialEntries: [url] },
  );
  render(<RouterProvider router={router} />);
  return router;
}

const card = (name: RegExp | string) => screen.getByRole("button", { name });
const dialog = () => screen.findByRole("dialog");

describe("Connectors cards", () => {
  it("a connected catalog slug shows once, as your row, beside the offered ones", () => {
    mount([row()]);
    expect(card("Alpha Mine: Connected")).toBeDefined();
    expect(card("BETA: available")).toBeDefined();
    expect(screen.queryByRole("button", { name: "ALPHA: available" })).toBeNull();
  });

  it("the lens chips narrow to connected or available and write the URL", async () => {
    const user = userEvent.setup();
    const router = mount([row()]);
    await user.click(screen.getByRole("button", { name: "Connected" }));
    expect(router.state.location.search).toBe("?filter=connected");
    expect(screen.queryByRole("button", { name: "BETA: available" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Available" }));
    expect(screen.queryByRole("button", { name: /Alpha Mine/ })).toBeNull();
    await user.click(screen.getByRole("button", { name: "All" }));
    expect(router.state.location.search).toBe("");
  });

  it("search narrows after the debounce and an empty result names the query", async () => {
    const user = userEvent.setup();
    mount([row()]);
    await user.type(await screen.findByPlaceholderText("Search connectors…"), "zzz");
    expect(await screen.findByText("No matches for “zzz”")).toBeDefined();
  });

  it("an empty connected lens points at the catalog", () => {
    mount([], "/connectors?filter=connected");
    expect(screen.getByText("No connectors here")).toBeDefined();
    expect(screen.getByText(/Connect a service from the catalog/)).toBeDefined();
  });

  it("the category funnel filters the grid and a chosen category rides the URL", async () => {
    const user = userEvent.setup();
    const router = mount([row()]);
    await user.click(screen.getByRole("button", { name: "Filter by category" }));
    await user.click(await screen.findByRole("menuitemradio", { name: "Dev" }));
    expect(router.state.location.search).toBe("?category=Dev");
    expect(screen.queryByRole("button", { name: /Alpha Mine/ })).toBeNull();
    expect(card("BETA: available")).toBeDefined();
  });
});

describe("Connectors one-click connect", () => {
  it("Connect on an offered card creates from the catalog and opens the new row", async () => {
    const user = userEvent.setup();
    vi.spyOn(window, "open").mockImplementation((() => null) as never);
    post.mockImplementation((() =>
      ok({
        connector: row({ id: "c7", catalog_slug: "beta", name: "Beta Mine" }),
        authorize_url: null,
      })) as never);
    mount([]);
    const beta = card("BETA: available");
    await user.click(within(beta).getByRole("button", { name: "Add" }));
    await waitFor(() => expect(post.mock.calls[0]?.[0]).toBe("/v1/connectors/connect"));
    expect(post.mock.calls[0]?.[1]).toMatchObject({ body: { catalog_slug: "beta" } });
    expect(await screen.findByRole("button", { name: "Beta Mine: Connected" })).toBeDefined();
  });

  it("an offered card opens the entry face as a modal and Escape closes it", async () => {
    const user = userEvent.setup();
    const router = mount([]);
    await user.click(card("ALPHA: available"));
    await dialog();
    expect(router.state.location.search).toBe("?connector=alpha");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(router.state.location.search).toBe(""));
  });
});

describe("Connector modal", () => {
  async function openMine(r: Row = row()) {
    const user = userEvent.setup();
    const router = mount([r]);
    await user.click(card(/Alpha Mine/));
    const dlg = await dialog();
    return { user, router, dlg };
  }

  it("a live row shows its URL and tools grouped read-only vs write", async () => {
    const { dlg } = await openMine();
    expect(within(dlg).getByText("https://alpha.example/mcp")).toBeDefined();
    expect(within(dlg).getByText("Read-only tools")).toBeDefined();
    expect(within(dlg).getByText("Write tools")).toBeDefined();
    expect(within(dlg).queryByRole("button", { name: "Connect" })).toBeNull();
  });

  it("picking Allow on a tool PATCHes the whole override map and folds the fresh row in", async () => {
    const { user, dlg } = await openMine();
    const groups = within(dlg).getAllByRole("group", { name: "Tool permission" });
    // groups: read-only bulk, read_doc, write bulk, write_doc
    await user.click(within(groups[3]!).getByRole("button", { name: "Allow" }));
    await waitFor(() => expect(patch).toHaveBeenCalledTimes(1));
    expect(patch.mock.calls[0]?.[0]).toBe("/v1/connectors/{connector_id}/policies");
    expect(patch.mock.calls[0]?.[1]).toMatchObject({ body: { policies: { write_doc: "allow" } } });
  });

  it("a group's bulk trio sets every row in that group", async () => {
    const { user, dlg } = await openMine();
    const groups = within(dlg).getAllByRole("group", { name: "Tool permission" });
    await user.click(within(groups[2]!).getByRole("button", { name: "Off" }));
    await waitFor(() => expect(patch).toHaveBeenCalled());
    expect(patch.mock.calls[0]?.[1]).toMatchObject({ body: { policies: { write_doc: "off" } } });
  });

  it("the auto-attach switch saves the dial", async () => {
    const { user, dlg } = await openMine();
    await user.click(within(dlg).getByRole("switch"));
    await waitFor(() => expect(patch).toHaveBeenCalled());
    expect(patch.mock.calls[0]?.[0]).toBe("/v1/connectors/{connector_id}");
    expect(patch.mock.calls[0]?.[1]).toMatchObject({ body: { auto_attach: true } });
  });

  it("Disable posts and a failed save toasts the server's words", async () => {
    const error = vi.spyOn(toast, "error");
    const { user, dlg } = await openMine();
    post.mockImplementation((() =>
      Promise.resolve({
        data: undefined,
        error: { error: { code: "x", message: "Can't right now." } },
        response: new Response(null, { status: 409 }),
      })) as never);
    await user.click(within(dlg).getByRole("button", { name: "Disable" }));
    await waitFor(() => expect(error).toHaveBeenCalledWith("Can't right now."));
    expect(post.mock.calls[0]?.[0]).toBe("/v1/connectors/{connector_id}/disable");
  });

  it("a disabled row offers Enable and hides its tools", async () => {
    const { user, dlg } = await openMine(row({ status: "disabled" }));
    expect(within(dlg).queryByText("Write tools")).toBeNull();
    await user.click(within(dlg).getByRole("button", { name: "Enable" }));
    await waitFor(() =>
      expect(post.mock.calls[0]?.[0]).toBe("/v1/connectors/{connector_id}/enable"),
    );
  });

  it("Remove deletes the row, closes the modal and drops the card", async () => {
    const { user, router, dlg } = await openMine();
    await user.click(within(dlg).getByRole("button", { name: "Remove Alpha Mine" }));
    await waitFor(() => expect(del).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(router.state.location.search).toBe(""));
    await waitFor(() => expect(screen.queryByRole("button", { name: /Alpha Mine/ })).toBeNull());
  });

  it("Retry probes a broken non-OAuth row and toasts the verdict", async () => {
    const success = vi.spyOn(toast, "success");
    const broken = row({ auth_kind: "none", status: "error", last_error: "timeout" });
    const { user, dlg } = await openMine(broken);
    post.mockImplementation((() => ok(row({ auth_kind: "none", status: "live" }))) as never);
    await user.click(within(dlg).getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(success).toHaveBeenCalledWith("Alpha Mine connected."));
    expect(post.mock.calls[0]?.[0]).toBe("/v1/connectors/{connector_id}/probe");
  });

  it("a row that needs re-authorization offers Reconnect, which starts the OAuth dance", async () => {
    vi.spyOn(window, "open").mockImplementation((() => null) as never);
    const { user, dlg } = await openMine(
      row({ status: "needs_reauth", last_error: "token expired" }),
    );
    expect(within(dlg).getByText("token expired")).toBeDefined();
    post.mockImplementation((() => ok({ authorize_url: "https://idp/c" })) as never);
    await user.click(within(dlg).getByRole("button", { name: "Reconnect" }));
    expect(await within(dlg).findByText("Open authorization page")).toBeDefined();
    expect(within(dlg).getByText("Waiting for the provider…")).toBeDefined();
    await user.click(within(dlg).getByRole("button", { name: "Cancel" }));
    expect(within(dlg).queryByText("Waiting for the provider…")).toBeNull();
  });
});

describe("Connectors OAuth landing", () => {
  it("a good landing toasts, opens the connector's modal and strips the param", async () => {
    const success = vi.spyOn(toast, "success");
    vi.spyOn(window, "close").mockImplementation(() => {});
    const router = mount([row()], "/connectors?connected=alpha");
    await waitFor(() => expect(success).toHaveBeenCalledWith("Connected."));
    await waitFor(() => expect(router.state.location.search).toBe("?connector=alpha"));
  });

  it("a failed landing says so and opens nothing", async () => {
    const error = vi.spyOn(toast, "error");
    vi.spyOn(window, "close").mockImplementation(() => {});
    const router = mount([row()], "/connectors?connected=alpha&ok=0");
    await waitFor(() => expect(error).toHaveBeenCalledWith("The connection didn't complete."));
    await waitFor(() => expect(router.state.location.search).toBe(""));
  });
});
