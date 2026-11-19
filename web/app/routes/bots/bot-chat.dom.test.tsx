/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// A bot's chat, driven: a one-chat bot opens straight into its thread, a many-chat bot lists
// chats once two exist; the thread shows user, bot, proposal and work-card rows, sends on Enter
// (keeping the draft on failure), answers proposals, anchors a reply to a card, and shows a
// live reply and the reconnect note. The conversation socket hook is mocked, not faked.
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "~/lib/api/client";

const h = vi.hoisted(() => ({
  rows: [] as unknown[],
  live: null as unknown,
  connected: true,
  refresh: vi.fn(() => Promise.resolve()),
}));
vi.mock("~/hooks/use-conversation-stream", () => ({
  useConversationStream: () => {
    const [rows, setRows] = useState(h.rows);
    return {
      state: { rows, lastSeq: 0, settledSeq: 0, live: h.live },
      connected: h.connected,
      ready: true,
      refresh: h.refresh,
      foldRows: (fn: (r: unknown[]) => unknown[]) => setRows(fn),
    };
  },
}));
// A card reading its task's inbox items is the work card's own test; here it would need a data router.
vi.mock("~/hooks/use-seen-task", () => ({ useSeenTask: () => {} }));

import { BotChat } from "./bot-chat";

const ok = (data: unknown) => ({
  data,
  error: undefined,
  response: new Response(null, { status: 200 }),
});
const failed = () => ({
  data: undefined,
  error: { error: { code: "x", message: "nope" } },
  response: new Response(null, { status: 500 }),
});

const convo = (id: string, over: Record<string, unknown> = {}) => ({
  id,
  title: `Chat ${id}`,
  unread: 0,
  last_message_at: null,
  project_id: null,
  ...over,
});
const msg = (seq: number, role: string, over: Record<string, unknown> = {}) => ({
  seq,
  role,
  kind: "text",
  text: "",
  detail: "",
  verdict: "answer",
  verdict_reason: "",
  status: "sent",
  gate_id: null,
  task_id: null,
  artifact_ids: [],
  meta: {},
  proposal: null,
  ...over,
});

let convos: unknown[];
let get: ReturnType<typeof vi.spyOn>;
let post: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  h.rows = [];
  h.live = null;
  h.connected = true;
  h.refresh.mockClear();
  convos = [convo("c1")];
  get = vi
    .spyOn(api, "GET")
    .mockImplementation(((path: string) =>
      Promise.resolve(
        path === "/v1/conversations" ? ok(convos) : path === "/v1/gates" ? ok([]) : ok({}),
      )) as never);
  post = vi
    .spyOn(api, "POST")
    .mockImplementation((() => Promise.resolve(ok({ verdict: "answer" }))) as never);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  document.body.innerHTML = "";
});

const mount = (mode = "many") =>
  render(
    <MemoryRouter>
      <BotChat name="scout" threadsMode={mode} />
    </MemoryRouter>,
  );

const sentBody = () => {
  const call = post.mock.calls.find(
    (c: unknown[]) => c[0] === "/v1/conversations/{conversation_id}/messages",
  ) as unknown[];
  return (call[1] as { body: Record<string, unknown> }).body;
};

describe("Opening the conversation", () => {
  it("lists this bot's conversations and lands in the first", async () => {
    mount();
    expect(await screen.findByRole("textbox", { name: "Message scout" })).toBeTruthy();
    expect(get).toHaveBeenCalledWith("/v1/conversations", { params: { query: { bot: "scout" } } });
  });

  it("offers to start one when there are none, and opens it", async () => {
    convos = [];
    post.mockResolvedValueOnce(ok(convo("c9")) as never);
    mount();
    await userEvent.setup().click(await screen.findByRole("button", { name: "Start one" }));
    expect(post).toHaveBeenCalledWith("/v1/conversations", { body: { bot: "scout" } });
    expect(await screen.findByRole("textbox", { name: "Message scout" })).toBeTruthy();
  });

  it("opens a one-chat bot straight into its conversation, with no New chat", async () => {
    post.mockResolvedValueOnce(ok(convo("c1")) as never);
    mount("one");
    expect(await screen.findByRole("textbox", { name: "Message scout" })).toBeTruthy();
    expect(post).toHaveBeenCalledWith("/v1/conversations", { body: { bot: "scout" } });
    expect(screen.queryByRole("button", { name: "New chat" })).toBeNull();
  });

  it("shows the chat list once there are two, and switches between them", async () => {
    convos = [convo("c1"), convo("c2", { unread: 2 })];
    mount();
    await screen.findByText("Chats");
    expect(screen.getByRole("button", { name: /Chat c1/ })).toBeTruthy();
    await userEvent.setup().click(screen.getByRole("button", { name: /Chat c2/ }));
    expect(screen.getByRole("textbox", { name: "Message scout" })).toBeTruthy();
  });

  it("New chat adds a conversation", async () => {
    post.mockResolvedValueOnce(ok(convo("c2")) as never);
    mount();
    await userEvent.setup().click(await screen.findByRole("button", { name: "New chat" }));
    expect(post).toHaveBeenCalledWith("/v1/conversations", { body: { bot: "scout" } });
    expect(await screen.findByText("Chats")).toBeTruthy();
  });
});

describe("The thread", () => {
  it("invites a first message in an empty thread", async () => {
    mount();
    expect(await screen.findByText("Say what you need.")).toBeTruthy();
  });

  it("shows user, bot and system lines, and why a message was refused", async () => {
    h.rows = [
      msg(1, "user", { text: "Hello bot" }),
      msg(2, "bot", { text: "Hello human", meta: { remembered: ["likes tea", "UK dates"] } }),
      msg(3, "user", { text: "Do evil", verdict: "refused", verdict_reason: "not allowed" }),
      msg(4, "system", { text: "The reply stalled" }),
    ];
    mount();
    expect(await screen.findByText("Hello bot")).toBeTruthy();
    expect(screen.getByText("Hello human")).toBeTruthy();
    expect(screen.getByText("Remembered: likes tea · UK dates")).toBeTruthy();
    expect(screen.getByText("Refused: not allowed")).toBeTruthy();
    expect(screen.getByText("The reply stalled")).toBeTruthy();
  });

  it("offers Save as file for a document-shaped reply and posts the save", async () => {
    h.rows = [msg(1, "bot", { text: "# Title\n\n## One\nx\n\n## Two\ny" })];
    mount();
    await userEvent.setup().click(await screen.findByRole("button", { name: "Save as file" }));
    expect(post).toHaveBeenCalledWith("/v1/conversations/{conversation_id}/messages/{seq}/save", {
      params: { path: { conversation_id: "c1", seq: 1 } },
      body: { format: "pdf" },
    });
    await waitFor(() => expect(h.refresh).toHaveBeenCalled());
  });

  it("shows the reply being written, with its tools", async () => {
    h.live = {
      bot: "scout",
      text: "Looking into it",
      ended: true,
      tools: [{ tool: "web.fetch", label: "Reading a page", done: true }],
    };
    mount();
    expect(await screen.findByText("Looking into it")).toBeTruthy();
    expect(screen.getByText("Reading a page")).toBeTruthy();
  });

  it("says the bot is replying while a reply is owed", async () => {
    h.rows = [msg(1, "user", { text: "Hi", verdict: "answer", status: "sent" })];
    mount();
    expect(await screen.findByText("scout is replying…")).toBeTruthy();
  });

  it("says it is reconnecting when the socket is down", async () => {
    h.connected = false;
    h.rows = [msg(1, "bot", { text: "Hi there" })];
    mount();
    expect(await screen.findByText("Reconnecting to the conversation…")).toBeTruthy();
  });

  it("asks the conversations list again once the first message is titled", async () => {
    h.rows = [msg(1, "user", { text: "Hi", status: "done" })];
    mount();
    await screen.findByText("Hi");
    await waitFor(() =>
      expect(get.mock.calls.filter((c: unknown[]) => c[0] === "/v1/conversations")).toHaveLength(2),
    );
  });
});

describe("Sending", () => {
  it("sends on Enter without starting a task, clears the box and reads the record back", async () => {
    const user = userEvent.setup();
    mount();
    const box = await screen.findByRole("textbox", { name: "Message scout" });
    await user.type(box, "Find leads{Enter}");
    expect(sentBody()).toEqual({ text: "Find leads", start_task: false, reply_to_task: null });
    await waitFor(() => expect((box as HTMLTextAreaElement).value).toBe(""));
    expect(h.refresh).toHaveBeenCalled();
  });

  it("Shift+Enter adds a line instead of sending", async () => {
    const user = userEvent.setup();
    mount();
    const box = await screen.findByRole("textbox", { name: "Message scout" });
    await user.type(box, "a{Shift>}{Enter}{/Shift}b");
    expect((box as HTMLTextAreaElement).value).toBe("a\nb");
    expect(post).not.toHaveBeenCalledWith(
      "/v1/conversations/{conversation_id}/messages",
      expect.anything(),
    );
  });

  it("keeps the draft when the send fails", async () => {
    const user = userEvent.setup();
    mount();
    const box = await screen.findByRole("textbox", { name: "Message scout" });
    post.mockResolvedValueOnce(failed() as never);
    await user.type(box, "Keep me");
    await user.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    expect((box as HTMLTextAreaElement).value).toBe("Keep me");
  });

  it("disables Send while the box is empty", async () => {
    mount();
    await screen.findByRole("textbox", { name: "Message scout" });
    expect((screen.getByRole("button", { name: "Send" }) as HTMLButtonElement).disabled).toBe(true);
  });
});

describe("Proposals", () => {
  const proposal = (status = "pending") => ({
    goal: "Audit the vendor list",
    why: "It is big",
    deliverable: "spreadsheet",
    direct: false,
    status,
    task_id: status === "started" ? "t7" : null,
  });

  it("Start posts the start and marks the card started", async () => {
    h.rows = [msg(1, "bot", { text: "Shall I?", proposal: proposal() })];
    mount();
    expect(await screen.findByText("Audit the vendor list")).toBeTruthy();
    expect(screen.getByText("spreadsheet")).toBeTruthy();
    await userEvent.setup().click(screen.getByRole("button", { name: "Start" }));
    expect(post).toHaveBeenCalledWith("/v1/conversations/{conversation_id}/proposals/{seq}/start", {
      params: { path: { conversation_id: "c1", seq: 1 } },
    });
    expect(await screen.findByText("started")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Not now" })).toBeNull();
  });

  it("Not now declines it", async () => {
    h.rows = [msg(1, "bot", { text: "Shall I?", proposal: proposal() })];
    mount();
    await userEvent.setup().click(await screen.findByRole("button", { name: "Not now" }));
    expect(post).toHaveBeenCalledWith(
      "/v1/conversations/{conversation_id}/proposals/{seq}/decline",
      { params: { path: { conversation_id: "c1", seq: 1 } } },
    );
    expect(await screen.findByText("declined")).toBeTruthy();
  });

  it("leaves a proposal pending when the start fails", async () => {
    post.mockResolvedValueOnce(failed() as never);
    h.rows = [msg(1, "bot", { text: "Shall I?", proposal: proposal() })];
    mount();
    await userEvent.setup().click(await screen.findByRole("button", { name: "Start" }));
    await waitFor(() => expect(post).toHaveBeenCalled());
    expect(await screen.findByRole("button", { name: "Not now" })).toBeTruthy();
  });

  it("links a started proposal to its task", async () => {
    h.rows = [msg(1, "bot", { text: "On it", proposal: proposal("started") })];
    mount();
    const link = await screen.findByRole("link", { name: /Details/ });
    expect(link.getAttribute("href")).toContain("t7");
  });
});

describe("Work cards in the thread", () => {
  const cardRows = () => [
    msg(1, "user", { text: "Find leads" }),
    msg(2, "system", { kind: "started", text: "Started a task: Find leads", task_id: "t1" }),
    msg(3, "system", { kind: "delivered", text: "Done", task_id: "t1", artifact_ids: ["a1"] }),
  ];

  beforeEach(() => {
    get.mockImplementation(((path: string) =>
      Promise.resolve(
        path === "/v1/conversations"
          ? ok(convos)
          : path === "/v1/tasks/{task_id}"
            ? ok({
                id: "t1",
                status: "delivered",
                goal: "Find leads",
                title: "Lead hunt",
                project_id: null,
                turn: 1,
                cost: { total_usd: "0" },
                plan: { steps: [] },
              })
            : ok([]),
      )) as never);
  });

  it("folds a task's rows into one card, and Reply anchors the next message to it", async () => {
    const user = userEvent.setup();
    h.rows = cardRows();
    mount();
    expect(await screen.findByText("Lead hunt")).toBeTruthy();
    expect(screen.getAllByText("Lead hunt")).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "Reply" }));
    expect(screen.getByText("Replying to: Lead hunt")).toBeTruthy();
    await user.type(screen.getByRole("textbox", { name: "Message scout" }), "Add a column{Enter}");
    expect(sentBody()).toEqual({ text: "Add a column", start_task: false, reply_to_task: "t1" });
    await waitFor(() => expect(screen.queryByText(/Replying to:/)).toBeNull());
  });

  it("clears the reply target on request", async () => {
    const user = userEvent.setup();
    h.rows = cardRows();
    mount();
    await user.click(await screen.findByRole("button", { name: "Reply" }));
    await user.click(screen.getByRole("button", { name: "Clear reply target" }));
    expect(screen.queryByText(/Replying to:/)).toBeNull();
  });
});
