/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// Share as template, driven: an unshared bot needs a byline before it can be shared, a 422
// naming URLs lists them and the next share acknowledges them, a shared bot shows its link and
// version and can publish an update or be unshared.
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "~/lib/api/client";
import { ShareTemplate } from "./share-template";

type Head = Parameters<typeof ShareTemplate>[0]["head"];
const head = { name: "scout", project_id: null } as unknown as Head;

const ok = (data: unknown) => ({
  data,
  error: undefined,
  response: new Response(null, { status: 200 }),
});
const published = {
  name: "scout",
  slug: "scout-x1",
  status: "published",
  template_version: 2,
  install_count: 3,
};

let templates: unknown[];
let post: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  templates = [];
  vi.spyOn(api, "GET").mockImplementation((() => Promise.resolve(ok(templates))) as never);
  post = vi
    .spyOn(api, "POST")
    .mockImplementation((() =>
      Promise.resolve(ok({ ...published, template_version: 1 }))) as never);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  document.body.innerHTML = "";
});

describe("Sharing an unshared bot", () => {
  it("keeps Share disabled until there is a byline, then posts it and shows the link", async () => {
    const user = userEvent.setup();
    render(<ShareTemplate head={head} />);
    const share = (await screen.findByRole("button", {
      name: "Share as template",
    })) as HTMLButtonElement;
    expect(share.disabled).toBe(true);
    await user.type(screen.getByRole("textbox", { name: "Byline" }), " Vamsi ");
    await user.click(share);
    expect(post).toHaveBeenCalledWith("/v1/templates/bots", {
      body: { name: "scout", project_id: null, author_name: "Vamsi", acknowledge_urls: false },
    });
    expect(await screen.findByText("/t/bots/scout-x1")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Publish update" })).toBeTruthy();
  });

  it("lists the URLs a 422 names, and the next share acknowledges them", async () => {
    const user = userEvent.setup();
    post.mockResolvedValueOnce({
      data: undefined,
      error: {
        error: {
          code: "urls_need_ack",
          message: "URLs need review",
          fields: { urls: "https://a.example/x, https://b.example" },
        },
      },
      response: new Response(null, { status: 422 }),
    } as never);
    render(<ShareTemplate head={head} />);
    await user.type(await screen.findByRole("textbox", { name: "Byline" }), "V");
    await user.click(screen.getByRole("button", { name: "Share as template" }));
    expect(await screen.findByText("https://a.example/x")).toBeTruthy();
    expect(screen.getByText("https://b.example")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Share as template" }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    expect(
      (post.mock.calls[1][1] as { body: { acknowledge_urls: boolean } }).body.acknowledge_urls,
    ).toBe(true);
    expect(await screen.findByText("/t/bots/scout-x1")).toBeTruthy();
    expect(screen.queryByText("https://b.example")).toBeNull();
  });

  it("stays unshared when the share fails without URLs", async () => {
    const user = userEvent.setup();
    post.mockResolvedValueOnce({
      data: undefined,
      error: { error: { code: "secret", message: "Looks like a secret" } },
      response: new Response(null, { status: 422 }),
    } as never);
    render(<ShareTemplate head={head} />);
    await user.type(await screen.findByRole("textbox", { name: "Byline" }), "V");
    await user.click(screen.getByRole("button", { name: "Share as template" }));
    await waitFor(() => expect(post).toHaveBeenCalled());
    expect(screen.queryByText(/\/t\/bots\//)).toBeNull();
    expect(screen.getByRole("button", { name: "Share as template" })).toBeTruthy();
  });
});

describe("A shared bot", () => {
  beforeEach(() => {
    templates = [published, { ...published, name: "other", slug: "other-1" }];
  });

  it("shows its link, version and install count", async () => {
    render(<ShareTemplate head={head} />);
    expect(await screen.findByText("/t/bots/scout-x1")).toBeTruthy();
    expect(screen.getByText(/v2 · installed 3×/)).toBeTruthy();
    expect(screen.queryByText("/t/bots/other-1")).toBeNull();
  });

  it("publishes an update under the same bot with its byline", async () => {
    const user = userEvent.setup();
    render(<ShareTemplate head={head} />);
    await user.type(await screen.findByRole("textbox", { name: "Byline" }), "Vamsi");
    await user.click(screen.getByRole("button", { name: "Publish update" }));
    await waitFor(() => expect(post).toHaveBeenCalled());
    expect((post.mock.calls[0][1] as { body: { author_name: string } }).body.author_name).toBe(
      "Vamsi",
    );
    expect(await screen.findByText(/v1/)).toBeTruthy();
  });

  it("unshares and goes back to asking for a byline", async () => {
    const user = userEvent.setup();
    render(<ShareTemplate head={head} />);
    await user.click(await screen.findByRole("button", { name: "Unshare" }));
    expect(post).toHaveBeenCalledWith("/v1/templates/bots/{slug}/unpublish", {
      params: { path: { slug: "scout-x1" } },
    });
    expect(await screen.findByRole("button", { name: "Share as template" })).toBeTruthy();
    expect(screen.queryByText("/t/bots/scout-x1")).toBeNull();
  });
});
