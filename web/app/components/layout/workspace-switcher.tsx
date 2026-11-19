/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The workspace switcher: the sidebar's head, a card. The face is the active workspace (its avatar, name
// and your role); the menu lists memberships as cards of the same anatomy, "Create workspace" and "Join with a code" (the invite email's code for
// someone who already has an account). A switch PUTs the session's workspace and reloads fully
// (lib/workspaces.switchWorkspace) so no loader cache survives it; a join accepts, then reloads into the workspace.
// With every membership gone (`me.workspace` null) the face says so and the same two items are the
// way back in.

import { Building2, ChevronsUpDown, Plus, Ticket, X } from "lucide-react";
import { useState } from "react";
import { toast } from "@prismoid/blocks/toast";
import { EmptyState, FormError } from "~/components/kit";
import { Button } from "@makeplane/propel/components/button";
import { Field } from "@makeplane/propel/components/field";
import { Icon } from "@makeplane/propel/components/icon";
import { IconButton } from "@makeplane/propel/components/icon-button";
import { Input, InputGroup } from "@makeplane/propel/components/input";
import {
  Dialog,
  DialogActions,
  DialogBody,
  DialogClose,
  DialogCloseGroup,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogHeading,
  DialogMain,
  DialogTitle,
} from "@makeplane/propel/components/dialog";
import {
  Menu,
  MenuContent,
  MenuGroup,
  MenuItem,
  MenuLabel,
  MenuSeparator,
  MenuTrigger,
} from "@makeplane/propel/components/menu";
import { WorkspaceAvatar } from "@makeplane/propel/components/workspace-avatar";
import { SidebarMenuButton } from "@prismoid/blocks/sidebar";
import { api, toastError, unwrap } from "~/lib/api/client";
import type { components } from "~/lib/api/types.gen";
import { joinWithCode, switchWorkspace } from "~/lib/workspaces";
import { capitalize, cn } from "~/lib/utils";

type WorkspaceBrief = components["schemas"]["WorkspaceBriefOut"];

export const CREATE_LABEL = "Create workspace";
export const JOIN_LABEL = "Join with a code";
export const NO_WORKSPACE_LABEL = "No workspace";

export function WorkspaceSwitcherFace({ workspace }: { workspace: WorkspaceBrief | null }) {
  return (
    <>
      {/* The workspace's own initials on its own colour: with several workspaces the face is
          told apart at a glance, which one building glyph for all of them never did. */}
      <WorkspaceAvatar size="md" alt={workspace?.name} />
      <span className="grid min-w-0 flex-1 text-left leading-tight">
        <span className="truncate text-body-sm-regular font-medium">
          {workspace?.name ?? NO_WORKSPACE_LABEL}
        </span>
        <span
          className={cn(
            "text-muted-foreground truncate text-caption-md-regular",
            workspace && "capitalize",
          )}
        >
          {workspace?.role ?? "Create or join one"}
        </span>
      </span>
      <ChevronsUpDown className="text-muted-foreground ml-auto size-4" />
    </>
  );
}

/** A workspace pane's body when every membership is gone: it names the switcher's way back in. */
export function NoWorkspaceState() {
  return (
    <EmptyState
      icon={Building2}
      title="You're not in a workspace"
      description={`Open the workspace menu at the top of the sidebar: ${CREATE_LABEL}, or ${JOIN_LABEL} from an invite.`}
    />
  );
}

/** The corner dismiss both dialogs wear. */
const DIALOG_CLOSE = (
  <DialogCloseGroup>
    <IconButton
      variant="ghost"
      size="xs"
      aria-label="Close"
      icon={<Icon icon={X} />}
      render={<DialogClose />}
    />
  </DialogCloseGroup>
);

export function WorkspaceList({
  workspaces,
  activeId,
  canCreate,
  onSwitch,
  onCreate,
  onJoin,
}: {
  workspaces: WorkspaceBrief[];
  activeId: string | null;
  /** `me.can_create_workspace`: false at the founding cap, when the item would only answer 403. */
  canCreate: boolean;
  onSwitch: (id: string) => void;
  onCreate: () => void;
  onJoin: () => void;
}) {
  return (
    <>
      {/* One membership per card: its avatar, its name, your role beneath, the check on the
          one you are in. */}
      <MenuGroup>
        <MenuLabel>Workspaces</MenuLabel>
        {workspaces.map((o) => (
          <MenuItem
            key={o.id}
            icon={<WorkspaceAvatar size="md" alt={o.name} />}
            label={o.name}
            description={capitalize(o.role)}
            selected={o.id === activeId}
            aria-current={o.id === activeId}
            onClick={() => o.id !== activeId && onSwitch(o.id)}
          />
        ))}
      </MenuGroup>
      <MenuSeparator />
      {canCreate && (
        <MenuItem label={CREATE_LABEL} icon={<Icon icon={Plus} />} onClick={onCreate} />
      )}
      <MenuItem label={JOIN_LABEL} icon={<Icon icon={Ticket} />} onClick={onJoin} />
    </>
  );
}

export function WorkspaceSwitcher({
  workspace,
  canCreate,
}: {
  workspace: WorkspaceBrief | null;
  canCreate: boolean;
}) {
  const [workspaces, setWorkspaces] = useState<WorkspaceBrief[]>(workspace ? [workspace] : []);
  const [creating, setCreating] = useState(false);
  const [joining, setJoining] = useState(false);

  const load = (open: boolean) => {
    if (!open) return;
    unwrap(api.GET("/v1/workspaces"))
      .then(setWorkspaces)
      .catch(() => {}); // the face still works with just the active workspace
  };
  const pick = (id: string) =>
    void switchWorkspace(id).catch((e) => toastError(e, "Couldn't switch workspace."));

  return (
    <>
      <Menu onOpenChange={load}>
        <MenuTrigger
          render={
            // A card, not a nav row: bordered and filled, so the head of the sidebar reads as
            // "the workspace you are in" and as something to press.
            <SidebarMenuButton size="lg" aria-label="Switch workspace" className="bg-card border">
              <WorkspaceSwitcherFace workspace={workspace} />
            </SidebarMenuButton>
          }
        />
        <MenuContent side="bottom" align="start" sideOffset={6}>
          <WorkspaceList
            workspaces={workspaces}
            activeId={workspace?.id ?? null}
            canCreate={canCreate}
            onSwitch={pick}
            onCreate={() => setCreating(true)}
            onJoin={() => setJoining(true)}
          />
        </MenuContent>
      </Menu>
      <CreateWorkspaceDialog open={creating} onOpenChange={setCreating} onCreated={pick} />
      <JoinWorkspaceDialog open={joining} onOpenChange={setJoining} />
    </>
  );
}

function CreateWorkspaceDialog({
  open,
  onOpenChange,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreated: (id: string) => void;
}) {
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);

  async function create() {
    setBusy(true);
    try {
      const workspace = await unwrap(api.POST("/v1/workspaces", { body: { name: name.trim() } }));
      toast.success("Workspace created.");
      onCreated(workspace.id);
    } catch (e) {
      toastError(e, "Couldn't create the workspace.");
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="xs">
        <DialogMain>
          <DialogHeader>
            <DialogHeading>
              <DialogTitle>Create workspace</DialogTitle>
            </DialogHeading>
          </DialogHeader>
          <DialogBody>
            <InputGroup size="lg">
              <Input
                size="lg"
                aria-label="Workspace name"
                value={name}
                onChange={(e) => setName(e.currentTarget.value)}
                placeholder="Acme"
                maxLength={120}
              />
            </InputGroup>
          </DialogBody>
        </DialogMain>
        <DialogActions>
          <Button
            variant="primary"
            size="md"
            stretch="auto"
            label="Create"
            disabled={busy || !name.trim()}
            onClick={() => void create()}
          />
        </DialogActions>
        {DIALOG_CLOSE}
      </DialogContent>
    </Dialog>
  );
}

/** The join dialog's inside: the pasted code, the server's refusal in words, the one button. */
export function JoinCodeView({
  code,
  error,
  busy,
  onCode,
  onJoin,
}: {
  code: string;
  error: string | null;
  busy: boolean;
  onCode: (v: string) => void;
  onJoin: () => void;
}) {
  return (
    <>
      <DialogMain>
        <DialogHeader>
          <DialogHeading>
            <DialogTitle>{JOIN_LABEL}</DialogTitle>
            <DialogDescription>Paste the one-time code from your invite email.</DialogDescription>
          </DialogHeading>
        </DialogHeader>
        <DialogBody>
          {/* Field carries the invalid state to the frame's danger border. */}
          <Field size="lg" invalid={error ? true : undefined}>
            <InputGroup size="lg">
              <Input
                size="lg"
                aria-label="Invite code"
                aria-invalid={error ? true : undefined}
                value={code}
                onChange={(e) => onCode(e.currentTarget.value)}
                placeholder="Invite code"
                autoComplete="off"
              />
            </InputGroup>
          </Field>
          <FormError>{error}</FormError>
        </DialogBody>
      </DialogMain>
      <DialogActions>
        <Button
          variant="primary"
          size="md"
          stretch="auto"
          label="Join"
          disabled={busy || !code.trim()}
          onClick={onJoin}
        />
      </DialogActions>
    </>
  );
}

function JoinWorkspaceDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function join() {
    setBusy(true);
    setError(null);
    try {
      const refused = await joinWithCode(code);
      if (refused) {
        setError(refused);
        setBusy(false);
      } // accepted: the page is reloading into the joined workspace
    } catch (e) {
      toastError(e, "Couldn't join the workspace.");
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="xs">
        <JoinCodeView
          code={code}
          error={error}
          busy={busy}
          onCode={setCode}
          onJoin={() => void join()}
        />
        {DIALOG_CLOSE}
      </DialogContent>
    </Dialog>
  );
}
