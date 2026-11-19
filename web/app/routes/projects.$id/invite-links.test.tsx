/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The owner's Invite links (ADR-062): the list with each link's state (a link is single-use),
// the create dialog whose result is the ONE time the full link is
// shown, and revoke. There is no DOM here,
// so the pieces are pure views over data plus two small wire actions, tested where they live.
import { isValidElement, type ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({ post: vi.fn(), del: vi.fn() }));
vi.mock("~/lib/api/client", async () => ({
  api: { POST: state.post, DELETE: state.del, GET: vi.fn() },
  toastError: vi.fn(),
  unwrap: async (p: Promise<{ err?: Error }>) => {
    const v = await p;
    if (v?.err) throw v.err;
    return v;
  },
}));

import {
  CLOSED_DIALOG,
  createInviteLink,
  CreatedLink,
  DEFAULT_CHOICE,
  EXPIRY_DAYS,
  InviteLinkRows,
  linkDialogReducer,
  NewLinkBody,
  revokeInviteLink,
} from "./invite-links";

const NOW = Date.parse("2026-10-01T12:00:00Z");
const inDays = (d: number) => new Date(NOW + d * 86_400_000).toISOString();

const link = (over: Record<string, unknown> = {}) => ({
  id: "i1",
  role: "editor" as const,
  uses: 0,
  expires_at: inDays(7),
  created_at: inDays(0),
  state: "live" as const,
  ...over,
});

/** Walk a React element tree (no rendering) for the element named `label`: by its aria-label,
 *  or by being the element whose text (children, or a Propel `label` prop) is exactly that. */
function byLabel(node: ReactNode, label: string): { onClick?: () => void } | null {
  if (!isValidElement(node)) {
    return Array.isArray(node) ? (node.map((n) => byLabel(n, label)).find(Boolean) ?? null) : null;
  }
  const props = node.props as Record<string, unknown>;
  if (props["aria-label"] === label || props.children === label || props.label === label) {
    return props as { onClick?: () => void };
  }
  return byLabel(props.children as ReactNode, label);
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
  state.post.mockReset();
  state.del.mockReset();
});
afterEach(() => vi.useRealTimers());

describe("the rows", () => {
  const rows = (links: ReturnType<typeof link>[], onRevoke = () => {}) =>
    InviteLinkRows({ links, failed: false, onRevoke, onRetry: () => {} });

  it("show role, when it expires, and its state - never a use count, a link is single-use", () => {
    const html = renderToStaticMarkup(<>{rows([link()])}</>);
    expect(html).toContain("editor");
    expect(html).not.toMatch(/\d+\/\d+/);
    expect(html).toContain("expires in 7d");
    expect(html).toContain("Live");
  });

  it("read a link minted for 7 days as 7 days left, not 6, a minute after it was made", () => {
    const soon = (ms: number) => new Date(NOW + ms).toISOString();
    const left = (ms: number) =>
      renderToStaticMarkup(<>{rows([link({ expires_at: soon(ms) })])}</>);
    expect(left(7 * 86_400_000 - 60_000)).toContain("expires in 7d");
    expect(left(25 * 3_600_000)).toContain("expires in 2d");
    expect(left(23 * 3_600_000)).toContain("expires in 23h");
  });

  it("say why a dead link is dead, and offer to revoke only a live one", () => {
    const html = renderToStaticMarkup(
      <>
        {rows([
          link({ id: "a", state: "expired", expires_at: inDays(-2) }),
          link({ id: "b", state: "used_up", uses: 1 }),
          link({ id: "c", state: "revoked" }),
        ])}
      </>,
    );
    expect(html).toContain("expired 2d ago");
    expect(html).toContain(">Used<");
    expect(html).toContain("Revoked");
    expect(html).not.toContain("Revoke ");
    expect(renderToStaticMarkup(<>{rows([link()])}</>)).toContain("Revoke editor link");
  });

  it("revoking a row asks for THAT link", () => {
    const onRevoke = vi.fn();
    const tree = rows([link({ id: "keep", role: "viewer" }), link({ id: "drop" })], onRevoke);
    byLabel(tree, "Revoke editor link")?.onClick?.();
    expect(onRevoke).toHaveBeenCalledExactlyOnceWith("drop");
  });

  it("no links yet is said, not left blank; loading shows nothing yet", () => {
    const view = (links: null | [], failed = false) =>
      renderToStaticMarkup(
        <>{InviteLinkRows({ links, failed, onRevoke: () => {}, onRetry: () => {} })}</>,
      );
    expect(view([])).toContain("No invite links yet");
    expect(view(null)).toBe("");
  });

  it("a list that failed to load says so and offers a retry - it never claims there are none", () => {
    const onRetry = vi.fn();
    const tree = InviteLinkRows({ links: null, failed: true, onRevoke: () => {}, onRetry });
    const html = renderToStaticMarkup(<>{tree}</>);
    expect(html).toContain("Couldn&#x27;t load the invite links");
    expect(html).not.toContain("No invite links yet");
    byLabel(tree, "Retry")?.onClick?.();
    expect(onRetry).toHaveBeenCalledOnce();
  });
});

describe("creating a link", () => {
  it("POSTs the chosen role and expiry, and hands back the full join URL", async () => {
    state.post.mockResolvedValue({ id: "i9", code: "si_abc123", role: "viewer" });
    const url = await createInviteLink("sp1", { role: "viewer", days: 30 }, "https://app.test");
    expect(state.post).toHaveBeenCalledWith("/v1/projects/{project_id}/invites", {
      params: { path: { project_id: "sp1" } },
      body: { role: "viewer", expires_in_days: 30 },
    });
    expect(url).toBe("https://app.test/join/si_abc123");
  });

  it("offers 1, 7 or 30 days, defaulting to an editor link for 7 days", () => {
    expect(EXPIRY_DAYS).toEqual([1, 7, 30]);
    expect(DEFAULT_CHOICE).toEqual({ role: "editor", days: 7 });
  });

  it("the dialog asks role and expiry, says the link lets one person in, and Create link fires the create", () => {
    const onCreate = vi.fn();
    const body = (
      <NewLinkBody
        choice={DEFAULT_CHOICE}
        onChoice={() => {}}
        url={null}
        busy={false}
        onCreate={onCreate}
      />
    );
    const html = renderToStaticMarkup(body);
    expect(html).toContain("Role");
    expect(html).toContain("Expires");
    expect(html).not.toContain("Uses");
    expect(html).toContain("One person can join with it");
    expect(html).not.toContain("This link is shown once.");
    byLabel(body.type(body.props), "Create link")?.onClick?.();
    expect(onCreate).toHaveBeenCalledOnce();
  });

  it("once made, the link is shown ONCE: read-only, copyable, and said so", () => {
    const html = renderToStaticMarkup(
      <NewLinkBody
        choice={DEFAULT_CHOICE}
        onChoice={() => {}}
        url="https://app.test/join/si_abc123"
        busy={false}
        onCreate={() => {}}
      />,
    );
    expect(html).toContain("https://app.test/join/si_abc123");
    expect(html).toMatch(/<input[^>]*readonly/i);
    expect(html).toContain("Copy invite link");
    expect(html).toContain("This link is shown once.");
    expect(html).not.toContain("Expires");
  });

  it("CreatedLink alone carries the URL in an input, never as page text a crawler could keep", () => {
    const html = renderToStaticMarkup(<CreatedLink url="https://app.test/join/si_x" />);
    expect(html).toMatch(/<input[^>]*value="https:\/\/app.test\/join\/si_x"/);
  });
});

describe("revoking a link", () => {
  it("DELETEs that invite under that Project", async () => {
    state.del.mockResolvedValue({});
    await revokeInviteLink("sp1", "i1");
    expect(state.del).toHaveBeenCalledWith(
      "/v1/projects/{project_id}/invites/{project_invite_id}",
      {
        params: { path: { project_id: "sp1", project_invite_id: "i1" } },
      },
    );
  });
});

describe("the create dialog's show-once state", () => {
  const reduce = linkDialogReducer;
  const made = { ...reduce(CLOSED_DIALOG, { type: "open" }) };

  it("opens with the defaults and no link, however it was left", () => {
    const dirty = reduce(
      reduce(reduce(CLOSED_DIALOG, { type: "open" }), {
        type: "choose",
        choice: { role: "viewer", days: 30 },
      }),
      { type: "close" },
    );
    const reopened = reduce(dirty, { type: "open" });
    expect(reopened.open).toBe(true);
    expect(reopened.choice).toEqual(DEFAULT_CHOICE);
    expect(reopened.url).toBeNull();
  });

  it("holds the link only while the dialog that made it is open", () => {
    const shown = reduce(made, { type: "made", gen: made.gen, url: "https://app.test/join/si_x" });
    expect(shown.url).toBe("https://app.test/join/si_x");
    const closed = reduce(shown, { type: "close" });
    expect(closed.url).toBeNull(); // the code is gone with the dialog
    expect(closed.choice).toEqual(DEFAULT_CHOICE);
    expect(closed.open).toBe(false);
  });

  it("a create that resolves after the dialog closed cannot bring its link back", () => {
    const closed = reduce(made, { type: "close" });
    const late = reduce(closed, {
      type: "made",
      gen: made.gen,
      url: "https://app.test/join/si_stale",
    });
    expect(late.url).toBeNull();
    expect(late).toEqual(closed);
  });

  it("nor into a dialog that was closed and opened again since", () => {
    const again = reduce(reduce(made, { type: "close" }), { type: "open" });
    const late = reduce(again, {
      type: "made",
      gen: made.gen,
      url: "https://app.test/join/si_stale",
    });
    expect(late.url).toBeNull();
    expect(again.gen).not.toBe(made.gen);
  });

  it("keeps the choice as it is picked", () => {
    const picked = reduce(made, { type: "choose", choice: { role: "viewer", days: 1 } });
    expect(picked.choice).toEqual({ role: "viewer", days: 1 });
  });
});
