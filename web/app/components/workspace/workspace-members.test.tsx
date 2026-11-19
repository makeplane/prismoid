/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { assignableRoles, canManageMember } from "~/lib/workspaces";
import { MembersView } from "./workspace-members";

const member = (id: string, role: string, name = id) => ({
  user: { id, email: `${id}@x.io`, name },
  role,
  joined_at: "2026-09-01T00:00:00Z",
});
const noop = () => {};
const handlers = {
  onRole: noop,
  onRemove: noop,
  onInvite: noop,
  onRevoke: noop,
  onLeave: noop,
  onTransfer: noop,
};

function view(role: string, members = [member("me", role), member("bob", "member")]) {
  return renderToStaticMarkup(
    <MembersView myId="me" role={role} members={members} invites={[]} {...handlers} />,
  );
}

describe("role gating (spec 4.5)", () => {
  it("an owner sees Invite, role menus and Transfer", () => {
    const html = view("owner");
    expect(html).toContain("Invite");
    expect(html).toContain('aria-label="Role for bob"');
    expect(html).toContain("Transfer");
  });

  it("an admin sees Invite and role menus but no Transfer", () => {
    const html = view("admin");
    expect(html).toContain("Invite");
    expect(html).toContain('aria-label="Role for bob"');
    expect(html).not.toContain("Transfer");
  });

  it("a member sees neither Invite nor role menus - and can leave", () => {
    const html = view("member");
    expect(html).not.toContain("Invite");
    expect(html).not.toContain("Role for");
    expect(html).toContain("Leave");
  });

  it("a guest sees only themselves", () => {
    const html = view("guest", [member("me", "guest", "Me Myself")]);
    expect(html).toContain("Me Myself");
    expect(html).not.toContain("bob");
    expect(html).not.toContain("Invite");
  });

  it("owner rows are an owner's to change alone", () => {
    expect(canManageMember("admin", "owner")).toBe(false);
    expect(canManageMember("owner", "owner")).toBe(true);
    expect(canManageMember("admin", "member")).toBe(true);
    expect(canManageMember("member", "guest")).toBe(false);
    expect(assignableRoles("admin")).toEqual(["guest", "member", "admin"]);
    expect(assignableRoles("owner")).toEqual(["guest", "member", "admin", "owner"]);
  });

  it("pending invites list for admins", () => {
    const html = renderToStaticMarkup(
      <MembersView
        myId="me"
        role="admin"
        members={[member("me", "admin")]}
        invites={[
          {
            id: "i1",
            email: "new@x.io",
            role: "member",
            expires_at: "2026-10-01T00:00:00Z",
            invited_by: null,
          },
        ]}
        {...handlers}
      />,
    );
    expect(html).toContain("new@x.io");
    expect(html).toContain("Revoke");
  });

  it("a pending invite says how long it has left, never 'ago'", () => {
    const inThreeDays = new Date(Date.now() + 3 * 86_400_000 + 60_000).toISOString();
    const html = renderToStaticMarkup(
      <MembersView
        myId="me"
        role="admin"
        members={[member("me", "admin")]}
        invites={[
          {
            id: "i1",
            email: "new@x.io",
            role: "member",
            expires_at: inThreeDays,
            invited_by: null,
          },
        ]}
        {...handlers}
      />,
    );
    expect(html).toContain("expires in 3d");
    expect(html).not.toMatch(/expires [^<]*ago/);
  });
});
