/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The artifact page offers Edit (and restore, and a label) only to a reader the server lets
// write: the task's `can_edit`, the same bit the task page's reader pane reads.
// A Project viewer, and a reader whose task did not load, get the read-only view: the server's
// `_require_edit_rights` would refuse their save.
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock("~/lib/api/client", async (orig) => ({
  ...(await orig<typeof import("~/lib/api/client")>()),
  api: { GET: state.get },
  unwrap: async (p: Promise<{ err?: Error }>) => {
    const v = await p;
    if (v?.err) throw v.err;
    return v;
  },
}));
vi.mock("~/components/artifact/artifact-view", () => ({
  ArtifactView: ({ readOnly }: { readOnly?: boolean }) => (
    <div data-read-only={String(Boolean(readOnly))} />
  ),
}));

import ArtifactDetail, { clientLoader } from "./route";

const A = "0b9f3c55-2f7e-4a8e-9f61-6a1f0e2c4d11";
const T = "62b53b15-c44c-455f-9c78-f752e2bf132e";

function wire(task: Record<string, unknown> | Error) {
  state.get.mockImplementation(async (path: string) => {
    if (path === "/v1/artifacts/{artifact_id}")
      return { title: "Memo", render: "markdown", task_id: T };
    if (path === "/v1/tasks/{task_id}") return task instanceof Error ? { err: task } : task;
    throw new Error(`unexpected GET ${path}`);
  });
}

async function page() {
  const data = await (clientLoader as unknown as (a: unknown) => Promise<unknown>)({
    params: { id: A },
  });
  const Page = ArtifactDetail as unknown as (p: {
    params: { id: string };
    loaderData: unknown;
  }) => React.ReactElement;
  return renderToStaticMarkup(<Page params={{ id: A }} loaderData={data} />);
}

describe("the artifact page's write affordances", () => {
  beforeEach(() => {
    state.get.mockReset();
  });

  it("are offered to a reader who may edit the task", async () => {
    wire({ id: T, title: "t", project_id: "s", can_edit: true });
    expect(await page()).toContain('data-read-only="false"');
  });

  it("are withheld from a Project viewer", async () => {
    wire({ id: T, title: "t", project_id: "s", can_edit: false });
    expect(await page()).toContain('data-read-only="true"');
  });

  it("are withheld when the task could not be read", async () => {
    wire(new Error("not found"));
    expect(await page()).toContain('data-read-only="true"');
  });
});
