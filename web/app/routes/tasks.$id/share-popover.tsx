/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The task header's Share button (ADR-062). A Project task says who can see it, lists them,
// and lets the Project owner mint an invite link; a personal task says it is private and offers
// the move that makes it shared. Everything is fetched when the popover OPENS, so a member
// added a minute ago is on the list and a closed popover costs nothing.

import { Button } from "@makeplane/propel/components/button";
import { Icon } from "@makeplane/propel/components/icon";
import { Input, InputGroup } from "@makeplane/propel/components/input";
import { Share2 } from "lucide-react";
import { useState } from "react";
import { useRevalidator } from "react-router";
import { toast } from "@prismoid/blocks/toast";
import { CopyButton } from "~/components/kit";
import { Popover, PopoverContent, PopoverTrigger } from "@makeplane/propel/components/popover";
import { useLoad } from "~/hooks/use-load";
import { api, toastError, unwrap } from "~/lib/api/client";
import { personName } from "~/lib/format";
import type { components } from "~/lib/api/types.gen";

type MemberRow = components["schemas"]["MemberOut"];
type ProjectOut = components["schemas"]["ProjectOut"];
type TaskDetailOut = components["schemas"]["TaskDetailOut"];

type Place = { id: string; name: string };

/** Where a personal task may be moved: the Projects the caller files into as an editor or owner.
 *  A viewer's Project would answer 403 (`steering.move` wants editor+ on the destination), so it
 *  is never offered. The list read's `role` is the caller's real one. */
export function moveTargets(projects: { id: string; name: string; role: string }[]): Place[] {
  return projects.filter((s) => s.role !== "viewer").map((s) => ({ id: s.id, name: s.name }));
}

/** The popover's body as a pure view over what was fetched. `project` null = a personal task. */
export function ShareBody({
  project,
  members,
  inviteUrl,
  inviting,
  onInvite,
  projects,
  pending,
  onMove,
  onConfirmMove,
  onCancelMove,
}: {
  project: {
    name: string;
    isOwner: boolean;
    /** The Project's owner, named to every member (the owner is a column, not a member row). */
    owner: { name: string; email: string | null }; // email null: a guest's read
  } | null;
  members: readonly MemberRow[] | null;
  inviteUrl: string | null;
  inviting: boolean;
  onInvite: () => void;
  /** Where a personal task could go (your Projects); null while loading. */
  projects: readonly Place[] | null;
  /** The Project a move is waiting on the person's word for, or null. */
  pending: Place | null;
  /** Picking a Project asks; it does not move. */
  onMove: (projectId: string) => void;
  onConfirmMove: () => void;
  onCancelMove: () => void;
}) {
  if (!project && pending) {
    // A move makes the task visible to everyone in the Project - never a one-tap surprise.
    return (
      <div className="space-y-2.5">
        <p className="text-body-sm-regular">
          {`Everyone in ${pending.name} will be able to see this task. Move it?`}
        </p>
        <div className="flex gap-2">
          <Button
            variant="primary"
            size="md"
            stretch="auto"
            onClick={onConfirmMove}
            label={`Move it to ${pending.name}`}
          />
          <Button variant="ghost" size="md" stretch="auto" onClick={onCancelMove} label="Cancel" />
        </div>
      </div>
    );
  }
  if (!project) {
    return (
      <div className="space-y-2.5">
        <p className="text-body-sm-regular">
          Only you can see this. Move it to a Project to work on it together.
        </p>
        {projects === null ? (
          <p className="text-muted-foreground text-caption-md-regular">Loading your Projects…</p>
        ) : projects.length === 0 ? (
          <p className="text-muted-foreground text-caption-md-regular">You have no Projects yet.</p>
        ) : (
          <ul className="space-y-0.5">
            {projects.map((s) => (
              <li key={s.id}>
                <Button
                  variant="ghost"
                  size="md"
                  stretch="full"
                  onClick={() => onMove(s.id)}
                  label={`Move to ${s.name}`}
                />
              </li>
            ))}
          </ul>
        )}
      </div>
    );
  }
  return (
    <div className="space-y-2.5">
      <p className="text-body-sm-regular">{`Everyone in ${project.name} can see this`}</p>
      {members === null ? (
        <p className="text-muted-foreground text-caption-md-regular">Loading members…</p>
      ) : (
        <ul className="space-y-1">
          <li className="flex items-baseline justify-between gap-3 text-caption-md-regular">
            <span className="min-w-0 truncate">
              {personName(project.owner.name || project.owner.email)}
            </span>
            <span className="text-muted-foreground shrink-0">owner</span>
          </li>
          {members.map((m) => (
            <li
              key={m.actor_id}
              className="flex items-baseline justify-between gap-3 text-caption-md-regular"
            >
              <span className="min-w-0 truncate">{personName(m.name || m.email)}</span>
              <span className="text-muted-foreground shrink-0">{m.role}</span>
            </li>
          ))}
        </ul>
      )}
      {project.isOwner &&
        (inviteUrl ? (
          <div className="space-y-1">
            <div className="flex items-center gap-1">
              <InputGroup size="md">
                <Input
                  size="md"
                  readOnly
                  value={inviteUrl}
                  aria-label="Invite link"
                  onFocus={(e) => e.currentTarget.select()}
                />
              </InputGroup>
              <CopyButton value={inviteUrl} label="Copy invite link" />
            </div>
            <p className="text-muted-foreground text-caption-md-regular">
              One person can join with this link, as an editor.
            </p>
          </div>
        ) : (
          <Button
            variant="secondary"
            size="md"
            stretch="auto"
            disabled={inviting}
            onClick={onInvite}
            label="Invite to Project"
          />
        ))}
    </div>
  );
}

/** The header's Share button and its popover. `detail` is only read for the task and its Project. */
export function SharePopover({ detail }: { detail: Pick<TaskDetailOut, "id" | "project_id"> }) {
  const projectId = detail.project_id;
  const revalidator = useRevalidator();
  const [open, setOpen] = useState(false);
  const [project, setProject] = useState<ProjectOut | null>(null);
  const [members, setMembers] = useState<MemberRow[] | null>(null);
  const [projects, setProjects] = useState<Place[] | null>(null);
  const [inviteUrl, setInviteUrl] = useState<string | null>(null);
  const [inviting, setInviting] = useState(false);
  const [pending, setPending] = useState<Place | null>(null);

  // A Project task reads its Project and members; a personal task reads where it could move.
  const path = projectId ? { project_id: projectId } : null;
  const { error: projectError } = useLoad(
    open && path
      ? (signal) => unwrap(api.GET("/v1/projects/{project_id}", { params: { path }, signal }))
      : null,
    [open, projectId],
    { onData: setProject },
  );
  const projectFailed = projectError !== undefined;
  useLoad(
    open && path
      ? (signal) =>
          unwrap(api.GET("/v1/projects/{project_id}/members", { params: { path }, signal }))
      : null,
    [open, projectId],
    { onData: setMembers, onError: () => setMembers([]) },
  );
  useLoad(
    open && !path
      ? (signal) => unwrap(api.GET("/v1/projects", { params: { query: { limit: 50 } }, signal }))
      : null,
    [open, projectId],
    {
      onData: (page) => setProjects(moveTargets(page.items)),
      onError: () => setProjects([]),
    },
  );

  async function invite() {
    if (!projectId || inviting) return;
    setInviting(true);
    try {
      const made = await unwrap(
        api.POST("/v1/projects/{project_id}/invites", {
          params: { path: { project_id: projectId } },
          // A link lets one person in; the expiry is the server's default.
          body: { role: "editor" },
        }),
      );
      setInviteUrl(`${window.location.origin}/join/${made.code}`);
    } catch (e) {
      toastError(e, "Couldn't create the invite link.");
    } finally {
      setInviting(false);
    }
  }

  async function move(toProjectId: string) {
    setPending(null);
    try {
      await unwrap(
        api.POST("/v1/tasks/{task_id}/move", {
          params: { path: { task_id: detail.id } },
          body: { project_id: toProjectId },
        }),
      );
      const name = projects?.find((s) => s.id === toProjectId)?.name;
      toast(name ? `Moved to ${name}` : "Moved");
      setOpen(false);
      // The loader's detail carries project_id - the page becomes a shared task.
      void revalidator.revalidate();
    } catch (e) {
      toastError(e, "Couldn't move the task.");
    }
  }

  return (
    <Popover
      open={open}
      onOpenChange={(o) => {
        setOpen(o);
        if (!o) setPending(null); // a half-made decision does not survive the popover
      }}
    >
      <PopoverTrigger
        render={
          <Button
            variant="ghost"
            size="sm"
            stretch="auto"
            aria-label="Share this task"
            icon={<Icon icon={Share2} />}
            label="Share"
          />
        }
      />
      <PopoverContent align="end">
        <div className="w-72">
          {projectId && !project ? (
            // The sentence names the Project and the owner's button hangs off its role: neither
            // is worth guessing at while the fetch is out.
            <p className="text-muted-foreground text-caption-md-regular">
              {projectFailed ? "Couldn't load this Project." : "Loading…"}
            </p>
          ) : (
            <ShareBody
              project={
                project
                  ? {
                      name: project.name,
                      isOwner: project.role === "owner",
                      owner: project.owner,
                    }
                  : null
              }
              members={members}
              inviteUrl={inviteUrl}
              inviting={inviting}
              onInvite={() => void invite()}
              projects={projects}
              pending={pending}
              onMove={(id) => setPending(projects?.find((s) => s.id === id) ?? null)}
              onConfirmMove={() => pending && void move(pending.id)}
              onCancelMove={() => setPending(null)}
            />
          )}
        </div>
      </PopoverContent>
    </Popover>
  );
}
