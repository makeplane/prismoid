/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// The revision history, driven: it hides until there are two versions, opens on click, viewing a
// version reports it, restoring mints a new head from the current one's hash, naming a version
// saves its label (and rolls back on failure), and a read-only reader gets neither lever.
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { toast } from "@prismoid/blocks/toast";
import { api, ApiError } from "~/lib/api/client";
import type { components } from "~/lib/api/types.gen";
import { RevisionHistory } from "./revision-history";

type ArtifactOut = components["schemas"]["ArtifactOut"];

const rev = (revision: number, sha256: string, over: Partial<ArtifactOut> = {}) =>
  ({
    id: `a${revision}`,
    task_id: "t",
    type: "document",
    title: "Notes",
    filename: "notes.md",
    render: "markdown",
    lineage_id: "l",
    label: null,
    bytes: 10,
    sha256,
    revision,
    provenance: { agent: "writer" },
    verdict: null,
    created_at: "2026-10-01T00:00:00Z",
    ...over,
  }) as unknown as ArtifactOut;

const LINEAGE = [rev(3, "ccc", { provenance: { agent: "user" } }), rev(2, "bbb"), rev(1, "aaa")];
const reply = (data: unknown) =>
  Promise.resolve({ data, error: undefined, response: new Response(null, { status: 200 }) });

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  document.body.innerHTML = "";
});

function mount(props: Partial<React.ComponentProps<typeof RevisionHistory>> = {}) {
  const onView = vi.fn();
  const onAdvance = vi.fn();
  render(
    <RevisionHistory
      revisions={LINEAGE}
      viewedId="a3"
      onView={onView}
      onAdvance={onAdvance}
      {...props}
    />,
  );
  return { onView, onAdvance };
}

async function open(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: /History/ }));
}

describe("RevisionHistory", () => {
  it("shows nothing for a single version", () => {
    const { container } = render(
      <RevisionHistory
        revisions={[LINEAGE[2]]}
        viewedId="a1"
        onView={vi.fn()}
        onAdvance={vi.fn()}
      />,
    );
    expect(container.innerHTML).toBe("");
  });

  it("opens to the story of each version and views one on click", async () => {
    const user = userEvent.setup();
    const { onView } = mount();
    await open(user);
    expect(await screen.findByText(/you · latest/)).toBeDefined();
    await user.click(screen.getAllByText(/writer ·/, { selector: "span" })[0]);
    expect(onView).toHaveBeenCalledWith("a2");
  });

  it("restoring posts the current head's hash and follows the new version", async () => {
    const user = userEvent.setup();
    const minted = rev(4, "aaa");
    const post = vi.spyOn(api, "POST").mockImplementation((() => reply(minted)) as never);
    const { onAdvance } = mount();
    await open(user);
    await user.click(await screen.findByRole("button", { name: "Restore v1" }));
    expect(await screen.findByText("Restore v1 as v4?")).toBeDefined();
    await user.click(screen.getByRole("button", { name: "Restore" }));
    await waitFor(() => expect(onAdvance).toHaveBeenCalledWith(minted));
    expect(post.mock.calls[0][0]).toBe("/v1/artifacts/{artifact_id}/restore");
    expect(post.mock.calls[0][1]).toEqual({
      params: { path: { artifact_id: "a1" } },
      body: { base_sha256: "ccc" },
    });
  });

  it("the head cannot be restored onto itself", async () => {
    const user = userEvent.setup();
    mount();
    await open(user);
    await screen.findByRole("button", { name: "Restore v1" });
    expect(screen.queryByRole("button", { name: "Restore v3" })).toBeNull();
  });

  it("a failed restore says so and does not advance", async () => {
    const user = userEvent.setup();
    const err = vi.spyOn(toast, "error").mockImplementation((() => "") as never);
    vi.spyOn(api, "POST").mockImplementation((() =>
      Promise.reject(new ApiError(409, "stale", "The lineage moved."))) as never);
    const { onAdvance } = mount();
    await open(user);
    await user.click(await screen.findByRole("button", { name: "Restore v2" }));
    await user.click(await screen.findByRole("button", { name: "Restore" }));
    await waitFor(() => expect(err).toHaveBeenCalledWith("The lineage moved."));
    expect(onAdvance).not.toHaveBeenCalled();
  });

  it("naming a version saves the label at once", async () => {
    const user = userEvent.setup();
    const post = vi.spyOn(api, "POST").mockImplementation((() => reply({})) as never);
    mount();
    await open(user);
    await user.click(await screen.findByRole("button", { name: "Label v2" }));
    await user.type(await screen.findByPlaceholderText("final draft"), "board draft{Enter}");
    expect(await screen.findByText(/board draft/)).toBeDefined();
    expect(post.mock.calls[0][1]).toEqual({
      params: { path: { artifact_id: "a2" } },
      body: { label: "board draft" },
    });
  });

  it("a label that fails to save rolls back", async () => {
    const user = userEvent.setup();
    vi.spyOn(toast, "error").mockImplementation((() => "") as never);
    vi.spyOn(api, "POST").mockImplementation((() => Promise.reject(new Error("x"))) as never);
    mount();
    await open(user);
    await user.click(await screen.findByRole("button", { name: "Label v2" }));
    await user.type(await screen.findByPlaceholderText("final draft"), "nope{Enter}");
    await waitFor(() => expect(screen.queryByText(/nope/)).toBeNull());
  });

  it("a read-only reader can download but neither label nor restore", async () => {
    const user = userEvent.setup();
    mount({ readOnly: true });
    await open(user);
    expect(await screen.findByRole("button", { name: "Download v2" })).toBeDefined();
    expect(screen.queryByRole("button", { name: "Label v2" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Restore v1" })).toBeNull();
  });

  it("download fetches the version's link and says so when it cannot", async () => {
    const user = userEvent.setup();
    const err = vi.spyOn(toast, "error").mockImplementation((() => "") as never);
    const get = vi
      .spyOn(api, "GET")
      .mockImplementation((() => Promise.reject(new Error("x"))) as never);
    mount();
    await open(user);
    await user.click(await screen.findByRole("button", { name: "Download v2" }));
    await waitFor(() =>
      expect(err).toHaveBeenCalledWith("Couldn't fetch that version's download link."),
    );
    expect(get.mock.calls[0][1]).toEqual({ params: { path: { artifact_id: "a2" } } });
  });
});
