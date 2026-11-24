/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// Workspace → Members: the roster, roles, invites, leave (spec 4.5). Gating is by role and
// the server enforces the same table: a member sees the roster and can leave; admin and above
// invite and change roles; owner roles and Transfer are owners' alone. A guest is served only
// themselves, so the view just draws what it is given.

import { useCallback, useEffect, useState } from "react";
import { toast } from "@prismoid/blocks/toast";
import { ConfirmAction } from "~/components/admin/confirm";
import { LoadingState, PageHeader, Rows, Section } from "~/components/kit";
import { NoWorkspaceState } from "~/components/layout/workspace-switcher";
import { Button } from "@makeplane/propel/components/button";
import {
  Dialog,
  DialogActions,
  DialogBody,
  DialogClose,
  DialogCloseGroup,
  DialogContent,
  DialogHeader,
  DialogHeading,
  DialogMain,
  DialogTitle,
  DialogTrigger,
} from "@makeplane/propel/components/dialog";
import { Icon } from "@makeplane/propel/components/icon";
import { IconButton } from "@makeplane/propel/components/icon-button";
import { Input, InputGroup } from "@makeplane/propel/components/input";
import { SelectField } from "@makeplane/propel/components/select-field";
import { X } from "lucide-react";
import { api, toastError, unwrap } from "~/lib/api/client";
import type { components } from "~/lib/api/types.gen";
import { ago, inAgo } from "~/lib/format";
import { capitalize } from "~/lib/utils";
import { assignableRoles, canManageMember, workspacePerms } from "~/lib/workspaces";
import { useMe } from "./use-workspace";

type Member = components["schemas"]["WorkspaceMemberOut"];
type Invite = components["schemas"]["WorkspaceInviteOut"];
type Act<A extends unknown[] = []> = (...a: A) => void | Promise<unknown>;

export function MembersView({
  myId,
  role,
  members,
  invites,
  onRole,
  onRemove,
  onInvite,
  onRevoke,
  onLeave,
  onTransfer,
}: {
  myId: string;
  role: string;
  members: Member[];
  invites: Invite[];
  onRole: Act<[actorId: string, role: string]>;
  onRemove: Act<[actorId: string]>;
  /** Resolves true once the invite is sent; a refusal (already toasted) resolves false. */
  onInvite: (email: string, role: string) => Promise<boolean>;
  onRevoke: Act<[inviteId: string]>;
  onLeave: Act;
  onTransfer: Act<[actorId: string]>;
}) {
  const perms = workspacePerms(role);
  return (
    <div className="space-y-6">
      <PageHeader
        as="h2"
        title="Members"
        description="Who is in this workspace, and what they can do."
      />
      <Section
        title="People"
        actions={perms.manage ? <InviteDialog role={role} onInvite={onInvite} /> : null}
      >
        <Rows as="ul">
          {members.map((m) => {
            const isMe = m.user.id === myId;
            return (
              <li
                key={m.user.id}
                className="flex items-center gap-3 px-4 py-3 text-body-sm-regular"
              >
                <div className="min-w-0 flex-1">
                  <p className="truncate font-medium">{m.user.name || m.user.email}</p>
                  <p className="text-muted-foreground truncate text-caption-md-regular">
                    {m.user.email} · joined {ago(m.joined_at)}
                  </p>
                </div>
                {!isMe && canManageMember(role, m.role) ? (
                  <SelectField<string>
                    modal={false}
                    size="md"
                    variant="secondary"
                    width="fit"
                    aria-label={`Role for ${m.user.name || m.user.email}`}
                    value={m.role}
                    onValueChange={(v) => v && v !== m.role && void onRole(m.user.id, v)}
                    options={assignableRoles(role).map((r) => ({ value: r, label: capitalize(r) }))}
                  />
                ) : (
                  <span className="text-muted-foreground text-caption-md-regular capitalize">
                    {m.role}
                  </span>
                )}
                {!isMe && perms.transfer && m.role !== "owner" && (
                  <ConfirmAction
                    trigger="Transfer"
                    title={`Make ${m.user.email} an owner?`}
                    description="They become an owner and you step down to admin."
                    onConfirm={async () => void (await onTransfer(m.user.id))}
                  />
                )}
                {!isMe && canManageMember(role, m.role) && (
                  <ConfirmAction
                    trigger="Remove"
                    destructive
                    title={`Remove ${m.user.email}?`}
                    description="They lose access. What they made stays in the workspace."
                    onConfirm={async () => void (await onRemove(m.user.id))}
                  />
                )}
                {isMe && (
                  <ConfirmAction
                    trigger="Leave"
                    destructive
                    title="Leave this workspace?"
                    description="You lose access. What you made stays here."
                    onConfirm={async () => void (await onLeave())}
                  />
                )}
              </li>
            );
          })}
        </Rows>
      </Section>
      {perms.manage && invites.length > 0 && (
        <Section title="Pending invites">
          <Rows as="ul">
            {invites.map((i) => (
              <li key={i.id} className="flex items-center gap-3 px-4 py-3 text-body-sm-regular">
                <div className="min-w-0 flex-1">
                  <p className="truncate font-medium">{i.email}</p>
                  <p className="text-muted-foreground text-caption-md-regular capitalize">
                    {i.role} · expires {inAgo(i.expires_at)}
                  </p>
                </div>
                <Button
                  variant="danger-outline"
                  size="sm"
                  stretch="auto"
                  label="Revoke"
                  onClick={() => void onRevoke(i.id)}
                />
              </li>
            ))}
          </Rows>
        </Section>
      )}
    </div>
  );
}

function InviteDialog({
  role,
  onInvite,
}: {
  role: string;
  onInvite: (email: string, role: string) => Promise<boolean>;
}) {
  const [open, setOpen] = useState(false);
  const [email, setEmail] = useState("");
  const [as, setAs] = useState("member");
  const [busy, setBusy] = useState(false);

  async function send() {
    setBusy(true);
    try {
      // A refused invite keeps the dialog and the typed address, so the person can fix and resend.
      if (!(await onInvite(email.trim(), as))) return;
      setOpen(false);
      setEmail("");
    } finally {
      setBusy(false);
    }
  }
  // An invite can never mint an owner (the door 422s it) - offer everything else the caller may.
  const roles = assignableRoles(role).filter((r) => r !== "owner");

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger
        render={<Button variant="primary" size="sm" stretch="auto" label="Invite" />}
      />
      <DialogContent size="xs">
        <DialogMain>
          <DialogHeader>
            <DialogHeading>
              <DialogTitle>Invite to this workspace</DialogTitle>
            </DialogHeading>
          </DialogHeader>
          <DialogBody>
            <div className="flex flex-col gap-4">
              <InputGroup size="lg">
                <Input
                  size="lg"
                  aria-label="Email"
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.currentTarget.value)}
                  placeholder="them@example.com"
                />
              </InputGroup>
              <SelectField<string>
                modal={false}
                size="lg"
                variant="secondary"
                width="fit"
                aria-label="Role"
                value={as}
                onValueChange={(v) => v && setAs(v)}
                options={roles.map((r) => ({ value: r, label: capitalize(r) }))}
              />
            </div>
          </DialogBody>
        </DialogMain>
        <DialogActions>
          <Button
            variant="primary"
            size="md"
            stretch="auto"
            label="Send invite"
            disabled={busy || !email.trim()}
            onClick={() => void send()}
          />
        </DialogActions>
        <DialogCloseGroup>
          <IconButton
            variant="ghost"
            size="xs"
            aria-label="Close"
            icon={<Icon icon={X} />}
            render={<DialogClose />}
          />
        </DialogCloseGroup>
      </DialogContent>
    </Dialog>
  );
}

export function WorkspaceMembersPanel() {
  const me = useMe();
  const [members, setMembers] = useState<Member[] | null>(null);
  const [invites, setInvites] = useState<Invite[]>([]);
  const workspaceId = me?.workspace?.id;
  const manage = me?.workspace ? workspacePerms(me.workspace.role).manage : false;
  const path = { params: { path: { workspace_id: workspaceId ?? "" } } };

  const load = useCallback(() => {
    if (!workspaceId) return;
    const p = { params: { path: { workspace_id: workspaceId } } };
    unwrap(api.GET("/v1/workspaces/{workspace_id}/members", p))
      .then(setMembers)
      .catch(() => toast.error("Couldn't load members."));
    if (manage)
      unwrap(api.GET("/v1/workspaces/{workspace_id}/invites", p))
        .then(setInvites)
        .catch(() => {});
  }, [workspaceId, manage]);
  useEffect(load, [load]);

  if (!me) return <LoadingState />;
  const workspace = me.workspace;
  if (!workspace) return <NoWorkspaceState />;
  if (!members) return <LoadingState />;

  /** Call a door, say how it went, reload the roster: true when it went through. */
  const run = (fn: () => Promise<unknown>, fail: string, ok?: string) =>
    fn().then(
      () => {
        if (ok) toast.success(ok);
        load();
        return true;
      },
      (e) => {
        toastError(e, fail);
        return false;
      },
    );
  const member = (actor_id: string) => ({
    params: { path: { workspace_id: workspace.id, actor_id } },
  });

  return (
    <MembersView
      myId={me.id}
      role={workspace.role}
      members={members}
      invites={invites}
      onRole={(id, role) =>
        run(
          () =>
            unwrap(
              api.PATCH("/v1/workspaces/{workspace_id}/members/{actor_id}", {
                ...member(id),
                body: { role: role as never },
              }),
            ),
          "Couldn't change the role.",
        )
      }
      onRemove={(id) =>
        run(
          () => unwrap(api.DELETE("/v1/workspaces/{workspace_id}/members/{actor_id}", member(id))),
          "Couldn't remove them.",
          "Removed.",
        )
      }
      onInvite={(email, role) =>
        run(
          () =>
            unwrap(
              api.POST("/v1/workspaces/{workspace_id}/invites", {
                ...path,
                body: { email, role: role as never },
              }),
            ),
          "Couldn't send the invite.",
          "Invite sent.",
        )
      }
      onRevoke={(invite_id) =>
        run(
          () =>
            unwrap(
              api.DELETE("/v1/workspaces/{workspace_id}/invites/{invite_id}", {
                params: { path: { workspace_id: workspace.id, invite_id } },
              }),
            ),
          "Couldn't revoke the invite.",
        )
      }
      onLeave={async () => {
        await unwrap(api.POST("/v1/workspaces/{workspace_id}/members/me/leave", path)); // a 409 (last owner, last workspace) surfaces in the dialog's toast
        window.location.reload(); // the session rebinds to a remaining workspace
      }}
      onTransfer={(actor_id) =>
        run(
          () =>
            unwrap(
              api.POST("/v1/workspaces/{workspace_id}/transfer", { ...path, body: { actor_id } }),
            ),
          "Couldn't transfer ownership.",
          "Ownership transferred.",
        )
      }
    />
  );
}
