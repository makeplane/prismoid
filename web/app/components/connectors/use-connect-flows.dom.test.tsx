/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// The OAuth connect dance as a hook: the consent tab is claimed inside the click and aimed once
// the URL arrives, a one-click catalog connect re-keys its flow from slug to row id, the poll
// settles a waiting flow when the row leaves authorizing, and every failure closes the tab.
import { toast } from "@prismoid/blocks/toast";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";
import { api } from "~/lib/api/client";
import { onConnectorsChanged } from "~/lib/settings-bus";
import { useConnectFlows } from "./use-connect-flows";
import { CONNECTOR_POLL_MS, type ConnectorRow } from "./vocabulary";

const ok = (data: unknown) =>
  Promise.resolve({ data, error: undefined, response: new Response(null, { status: 200 }) });
const fail = (message: string) =>
  Promise.resolve({
    data: undefined,
    error: { error: { code: "x", message } },
    response: new Response(null, { status: 400 }),
  });

const row = (over: Partial<ConnectorRow> = {}): ConnectorRow =>
  ({
    id: "c1",
    name: "Linear",
    slug: "linear",
    status: "live",
    last_error: null,
    ...over,
  }) as ConnectorRow;

type Tab = {
  opener: unknown;
  close: ReturnType<typeof vi.fn>;
  location: { replace: ReturnType<typeof vi.fn> };
};
let tab: Tab;
let post: ReturnType<typeof vi.spyOn>;
let changed: Mock<() => void>;
let off: () => void;

beforeEach(() => {
  tab = { opener: "x", close: vi.fn(), location: { replace: vi.fn() } };
  vi.spyOn(window, "open").mockImplementation((() => tab) as never);
  post = vi.spyOn(api, "POST");
  changed = vi.fn<() => void>();
  off = onConnectorsChanged(() => changed());
});
afterEach(() => {
  off();
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("connectExisting", () => {
  it("claims a tab with its opener severed, aims it at the authorize URL and waits", async () => {
    post.mockImplementation((() => ok({ authorize_url: "https://idp/consent" })) as never);
    const { result } = renderHook(() => useConnectFlows());
    await act(async () => {
      await result.current.connectExisting("c1");
    });
    expect(tab.opener).toBeNull();
    expect(tab.location.replace).toHaveBeenCalledWith("https://idp/consent");
    expect(post.mock.calls[0]?.[0]).toBe("/v1/connectors/{connector_id}/authorize");
    expect(post.mock.calls[0]?.[1]).toMatchObject({ credentials: "include" });
    expect(result.current.flows.c1).toMatchObject({
      phase: "waiting",
      url: "https://idp/consent",
      opened: true,
    });
  });

  it("a blocked popup leaves the flow unopened so the UI can offer the link", async () => {
    vi.spyOn(window, "open").mockImplementation((() => null) as never);
    post.mockImplementation((() => ok({ authorize_url: "https://idp/consent" })) as never);
    const { result } = renderHook(() => useConnectFlows());
    await act(async () => {
      await result.current.connectExisting("c1");
    });
    expect(result.current.flows.c1?.opened).toBe(false);
  });

  it("a failed authorize closes the tab, drops the flow and toasts", async () => {
    const error = vi.spyOn(toast, "error");
    post.mockImplementation((() => fail("Not allowed.")) as never);
    const { result } = renderHook(() => useConnectFlows());
    await act(async () => {
      await result.current.connectExisting("c1");
    });
    expect(tab.close).toHaveBeenCalled();
    expect(result.current.flows).toEqual({});
    expect(error).toHaveBeenCalledWith("Not allowed.");
  });

  it("cancel drops a waiting flow", async () => {
    post.mockImplementation((() => ok({ authorize_url: "https://idp/consent" })) as never);
    const { result } = renderHook(() => useConnectFlows());
    await act(async () => {
      await result.current.connectExisting("c1");
    });
    act(() => result.current.cancel("c1"));
    expect(result.current.flows).toEqual({});
  });
});

describe("connectFromCatalog", () => {
  it("re-keys the flow from the slug to the new row id and announces the change", async () => {
    post.mockImplementation((() =>
      ok({
        connector: row({ id: "c9", status: "authorizing" }),
        authorize_url: "https://idp/x",
      })) as never);
    const { result } = renderHook(() => useConnectFlows());
    let created: ConnectorRow | null = null;
    await act(async () => {
      created = await result.current.connectFromCatalog("linear");
    });
    expect((created as ConnectorRow | null)?.id).toBe("c9");
    expect(Object.keys(result.current.flows)).toEqual(["c9"]);
    expect(post.mock.calls[0]?.[1]).toMatchObject({ body: { catalog_slug: "linear" } });
    expect(changed).toHaveBeenCalled();
  });

  it("an already-live server needs no consent: tab closed, success toast, no flow", async () => {
    const success = vi.spyOn(toast, "success");
    post.mockImplementation((() => ok({ connector: row(), authorize_url: null })) as never);
    const { result } = renderHook(() => useConnectFlows());
    await act(async () => {
      await result.current.connectFromCatalog("linear");
    });
    expect(tab.close).toHaveBeenCalled();
    expect(result.current.flows).toEqual({});
    expect(success).toHaveBeenCalledWith("Linear connected.");
  });

  it("a row that came back broken toasts its own error", async () => {
    const error = vi.spyOn(toast, "error");
    post.mockImplementation((() =>
      ok({
        connector: row({ status: "error", last_error: "Handshake failed." }),
        authorize_url: null,
      })) as never);
    const { result } = renderHook(() => useConnectFlows());
    await act(async () => {
      await result.current.connectFromCatalog("linear");
    });
    expect(error).toHaveBeenCalledWith("Handshake failed.");
  });

  it("a failed connect returns null and leaves no flow behind", async () => {
    post.mockImplementation((() => fail("Nope.")) as never);
    const { result } = renderHook(() => useConnectFlows());
    let created: ConnectorRow | null = row();
    await act(async () => {
      created = await result.current.connectFromCatalog("linear");
    });
    expect(created).toBeNull();
    expect(result.current.flows).toEqual({});
  });
});

describe("the watch", () => {
  async function waiting(onRows?: (rows: ConnectorRow[]) => void) {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    post.mockImplementation((() => ok({ authorize_url: "https://idp/c" })) as never);
    const hook = renderHook(() => useConnectFlows({ onRows }));
    await act(async () => {
      await hook.result.current.connectExisting("c1");
    });
    return hook;
  }
  const tick = () =>
    act(async () => void (await vi.advanceTimersByTimeAsync(CONNECTOR_POLL_MS + 10)));

  it("keeps waiting while the row is authorizing, and hands each roster to the caller", async () => {
    const onRows = vi.fn();
    vi.spyOn(api, "GET").mockImplementation((() =>
      ok({ items: [row({ status: "authorizing" })] })) as never);
    const { result } = await waiting(onRows);
    await tick();
    expect(onRows).toHaveBeenCalled();
    expect(result.current.flows.c1?.phase).toBe("waiting");
  });

  it("settles on live: drops the flow, toasts and announces", async () => {
    const success = vi.spyOn(toast, "success");
    vi.spyOn(api, "GET").mockImplementation((() => ok({ items: [row()] })) as never);
    const { result } = await waiting();
    await tick();
    await waitFor(() => expect(result.current.flows).toEqual({}));
    expect(success).toHaveBeenCalledWith("Linear connected.");
    expect(changed).toHaveBeenCalled();
  });

  it("settles a failed row with its last error", async () => {
    const error = vi.spyOn(toast, "error");
    vi.spyOn(api, "GET").mockImplementation((() =>
      ok({ items: [row({ status: "error", last_error: "Denied by user." })] })) as never);
    const { result } = await waiting();
    await tick();
    await waitFor(() => expect(result.current.flows).toEqual({}));
    expect(error).toHaveBeenCalledWith("Denied by user.");
  });

  it("a row removed while waiting just ends the watch quietly", async () => {
    const error = vi.spyOn(toast, "error");
    vi.spyOn(api, "GET").mockImplementation((() => ok({ items: [] })) as never);
    const { result } = await waiting();
    await tick();
    await waitFor(() => expect(result.current.flows).toEqual({}));
    expect(error).not.toHaveBeenCalled();
  });
});
