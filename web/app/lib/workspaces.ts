/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The workspace's vocabulary on the web (spec 4.5): who may do what and the one
// switch verb. Pure, so the gating is testable without a DOM.

import { api, ApiError, unwrap } from "~/lib/api/client";

export type WorkspaceRole = "guest" | "member" | "admin" | "owner";
const RANK: Record<string, number> = { guest: 0, member: 1, admin: 2, owner: 3 };
const rank = (role: string) => RANK[role] ?? -1;

/** What a role may draw - mirrors the server's table, which stays the enforcer. */
export function workspacePerms(role: string) {
  return {
    rename: rank(role) >= RANK.owner,
    transfer: rank(role) >= RANK.owner,
    manage: rank(role) >= RANK.admin, // members, invites
    seeRoster: rank(role) >= RANK.member,
  };
}

/** Admin and above manage members; a row that holds an owner role is an owner's to change. */
export function canManageMember(myRole: string, theirRole: string): boolean {
  if (rank(myRole) < RANK.admin) return false;
  return theirRole !== "owner" || myRole === "owner";
}

/** The roles the caller may hand out - owner only by owners. */
export function assignableRoles(myRole: string): WorkspaceRole[] {
  const base: WorkspaceRole[] = ["guest", "member", "admin"];
  return myRole === "owner" ? [...base, "owner"] : base;
}

export type SwitchIO = {
  put: (workspaceId: string) => Promise<unknown>;
  reload: () => void;
};

const live: SwitchIO = {
  put: (workspace_id) => unwrap(api.PUT("/v1/auth/session/workspace", { body: { workspace_id } })),
  reload: () => window.location.reload(),
};

/** Switch the session's active workspace, then reload fully - no loader cache may outlive it. */
export async function switchWorkspace(workspaceId: string, io: SwitchIO = live): Promise<void> {
  await io.put(workspaceId);
  io.reload();
}

export type JoinIO = {
  accept: (code: string) => Promise<unknown>;
  reload: () => void;
};

const liveJoin: JoinIO = {
  // The accept also moves this session into the joined workspace, so a reload lands there.
  accept: (code) => unwrap(api.POST("/v1/workspaces/invites/accept", { body: { code } })),
  reload: () => window.location.reload(),
};

/** Join a workspace with an emailed invite code. Resolves to null once accepted (and reloading into
 *  the workspace), or to the server's refusal - wrong email, expired, used - worded by the server. */
export async function joinWithCode(code: string, io: JoinIO = liveJoin): Promise<string | null> {
  const trimmed = code.trim();
  if (!trimmed) return "Paste the code from your invite email.";
  try {
    await io.accept(trimmed);
  } catch (e) {
    if (e instanceof ApiError) return e.message;
    throw e;
  }
  io.reload();
  return null;
}
