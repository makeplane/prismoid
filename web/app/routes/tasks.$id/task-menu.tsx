/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The task page's ⋯ menu - THE task actions: pause/resume and cancel while live (moved in
// from the status row), rename in any state (a label is not a machine state),
// and delete - destructive and terminal-only: a running task's workflow is still
// writing, so the API refuses it and this UI offers Cancel first instead.

import {
  Archive,
  ArchiveRestore,
  GitBranch,
  MoreHorizontal,
  Pause,
  Pencil,
  Play,
  Trash2,
  X,
} from "lucide-react";
import { useState } from "react";
import { useNavigate, useRevalidator } from "react-router";
import { useTaskWindow } from "~/components/tasks/task-window";
import {
  AlertDialog,
  AlertDialogActions,
  AlertDialogBody,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogHeader,
  AlertDialogIntro,
  AlertDialogTitle,
} from "@makeplane/propel/components/alert-dialog";
import { Button } from "@makeplane/propel/components/button";
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
import { Icon } from "@makeplane/propel/components/icon";
import { IconButton } from "@makeplane/propel/components/icon-button";
import { Input, InputGroup } from "@makeplane/propel/components/input";
import {
  Menu,
  MenuContent,
  MenuItem,
  MenuSeparator,
  MenuTrigger,
} from "@makeplane/propel/components/menu";
import { toastError, api, unwrap } from "~/lib/api/client";
import { TERMINAL, type TaskStatus } from "~/lib/api/events";

/** What the caller may do to this task, straight off the detail read. */
export type TaskAccess = {
  /** Steer, pause, cancel, follow up, fork: an editor of the Project (or the owner of a personal task). */
  canEdit: boolean;
  /** Archive and delete: the task's creator, or the owner of its Project. */
  canCurate: boolean;
  /** Rename and pin: the creator alone. */
  isCreator: boolean;
};

/** Which verbs the ⋯ menu offers - only the ones the server would accept for THIS person, so a
 *  viewer never meets a Pause that answers 403. Cancel is for any live task; pause only stops a
 *  running or waiting one (a held or planning task answers 422) and resume only a paused one; the
 *  fork is offered whenever the caller may write. */
export function menuVerbs(a: TaskAccess & { status: TaskStatus }) {
  const control = !TERMINAL.has(a.status) && a.canEdit;
  const stoppable = a.status === "running" || a.status === "waiting";
  return {
    control,
    /** The one of pause and resume the server would take now, if either. */
    toggle: !control
      ? null
      : stoppable
        ? ("pause" as const)
        : a.status === "paused"
          ? ("resume" as const)
          : null,
    fork: a.canEdit,
    rename: a.isCreator,
    archive: a.canCurate,
    delete: a.canCurate,
  };
}

const TOGGLE = {
  pause: { icon: Pause, label: "Pause" },
  resume: { icon: Play, label: "Resume" },
} as const;

export function TaskMenu({
  onFork,
  taskId,
  title,
  goal,
  status,
  archivedAt,
  onControl,
  access,
}: {
  /** What the caller may do here; every verb below is offered only if the server would take it. */
  access: TaskAccess;
  taskId: string;
  title?: string | null;
  goal: string;
  status: TaskStatus;
  /** The archive stamp (null = active) - enables the Archive/Unarchive item. */
  archivedAt?: string | null;
  /** Pause/resume/cancel verbs, provided where the live machine is wired (the task page). */
  onControl?: (intent: "pause" | "resume" | "cancel") => void;
  /** Opens the fork dialog - the EXPLICIT branch gesture (user law v2): the
   *  whole thread's context forks into a new branch and continues from there. */
  onFork?: () => void;
}) {
  const revalidator = useRevalidator();
  const [renaming, setRenaming] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const live = !TERMINAL.has(status);
  const verbs = menuVerbs({ ...access, status });
  const toggle = verbs.toggle;

  async function toggleArchive() {
    try {
      await unwrap(
        api.POST(archivedAt ? "/v1/tasks/{task_id}/unarchive" : "/v1/tasks/{task_id}/archive", {
          params: { path: { task_id: taskId } },
        }),
      );
      revalidator.revalidate(); // the loader's detail carries archived_at
    } catch (e) {
      toastError(e, "Couldn't change the archive state.");
    }
  }

  // A menu with nothing in it (a viewer, on a task that is not theirs) is no menu at all.
  if (
    !(verbs.control && onControl) &&
    !(verbs.fork && onFork) &&
    !verbs.rename &&
    !verbs.archive &&
    !verbs.delete
  )
    return null;

  return (
    <>
      <Menu>
        <MenuTrigger
          render={
            <IconButton
              size="sm"
              variant="ghost"
              icon={<Icon icon={MoreHorizontal} />}
              aria-label="Task actions"
            />
          }
        />
        <MenuContent align="end">
          {verbs.control && onControl && (
            <>
              {toggle && (
                <MenuItem
                  icon={<Icon icon={TOGGLE[toggle].icon} />}
                  label={TOGGLE[toggle].label}
                  onClick={() => onControl(toggle)}
                />
              )}
              <MenuItem
                icon={<Icon icon={X} />}
                label="Cancel task"
                onClick={() => onControl("cancel")}
              />
              <MenuSeparator />
            </>
          )}
          {verbs.fork && onFork && (
            <MenuItem icon={<Icon icon={GitBranch} />} label="Fork this task" onClick={onFork} />
          )}
          {verbs.rename && (
            <MenuItem
              icon={<Icon icon={Pencil} />}
              label="Rename"
              onClick={() => setRenaming(true)}
            />
          )}
          {verbs.archive && (
            <MenuItem
              // Same rule as delete: archiving hides work, and live work must not vanish.
              disabled={live}
              title={live ? "Cancel the task before archiving it" : undefined}
              icon={<Icon icon={archivedAt ? ArchiveRestore : Archive} />}
              label={archivedAt ? "Unarchive" : "Archive"}
              onClick={() => !live && void toggleArchive()}
            />
          )}
          {verbs.delete && (
            <MenuItem
              variant="danger"
              // A live task cannot be deleted - the API would 409. Offer it only when terminal,
              // with the reason in a tooltip so the affordance is honest rather than absent.
              disabled={live}
              title={live ? "Cancel the task before deleting it" : undefined}
              icon={<Icon icon={Trash2} />}
              label="Delete"
              onClick={() => !live && setDeleting(true)}
            />
          )}
        </MenuContent>
      </Menu>

      {renaming && (
        <RenameDialog taskId={taskId} initial={title || goal} onClose={() => setRenaming(false)} />
      )}
      {deleting && (
        <DeleteDialog taskId={taskId} label={title || goal} onClose={() => setDeleting(false)} />
      )}
    </>
  );
}

function RenameDialog({
  taskId,
  initial,
  onClose,
}: {
  taskId: string;
  initial: string;
  onClose: () => void;
}) {
  const revalidator = useRevalidator();
  const [name, setName] = useState(initial.slice(0, 60));
  const [busy, setBusy] = useState(false);

  async function save() {
    const next = name.trim().slice(0, 60);
    if (!next || next === initial.trim()) {
      onClose();
      return;
    }
    setBusy(true);
    try {
      await unwrap(
        api.PATCH("/v1/tasks/{task_id}", {
          params: { path: { task_id: taskId } },
          body: { title: next },
        }),
      );
      // The breadcrumb reads the route loader's detail - revalidate so it shows the new name.
      revalidator.revalidate();
      onClose();
    } catch (e) {
      toastError(e, "Couldn't rename the task.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent size="xs">
        <DialogMain>
          <DialogHeader>
            <DialogHeading>
              <DialogTitle>Rename task</DialogTitle>
              <DialogDescription>
                A label for you - the original ask is kept unchanged.
              </DialogDescription>
            </DialogHeading>
          </DialogHeader>
          <DialogBody>
            <InputGroup size="lg">
              <Input
                size="lg"
                value={name}
                onChange={(e) => setName(e.currentTarget.value)}
                maxLength={60}
                autoFocus
                aria-label="Task name"
                onKeyDown={(e) => {
                  if (e.key === "Enter") void save();
                  if (e.key === "Escape") onClose();
                }}
              />
            </InputGroup>
          </DialogBody>
        </DialogMain>
        <DialogActions>
          <Button variant="ghost" size="md" stretch="auto" label="Cancel" onClick={onClose} />
          <Button
            variant="primary"
            size="md"
            stretch="auto"
            label="Save"
            onClick={() => void save()}
            disabled={busy || !name.trim()}
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

function DeleteDialog({
  taskId,
  label,
  onClose,
}: {
  taskId: string;
  label: string;
  onClose: () => void;
}) {
  const navigate = useNavigate();
  const { remove } = useTaskWindow();
  const [busy, setBusy] = useState(false);

  async function confirm() {
    setBusy(true);
    // Route through the task window so the sidebar row drops in the same motion.
    const ok = await remove(taskId);
    setBusy(false);
    if (ok) {
      onClose();
      navigate("/tasks"); // the task no longer exists - leave its page
    }
  }

  return (
    // A yes/no confirmation: the alert dialog (an outside press does not dismiss it).
    <AlertDialog open onOpenChange={(o) => !o && onClose()}>
      <AlertDialogContent>
        <AlertDialogBody>
          <AlertDialogHeader>
            <AlertDialogIntro>
              <AlertDialogTitle>Delete this task?</AlertDialogTitle>
              <AlertDialogDescription>
                “{label.slice(0, 80)}” and everything it produced - its artifacts, transcript, and
                plan - are permanently removed. Your usage history is kept. This cannot be undone.
              </AlertDialogDescription>
            </AlertDialogIntro>
          </AlertDialogHeader>
        </AlertDialogBody>
        <AlertDialogActions>
          <Button
            variant="ghost"
            size="md"
            stretch="auto"
            label="Cancel"
            onClick={onClose}
            disabled={busy}
          />
          <Button
            variant="danger"
            size="md"
            stretch="auto"
            icon={<Icon icon={Trash2} />}
            label="Delete task"
            onClick={() => void confirm()}
            disabled={busy}
          />
        </AlertDialogActions>
      </AlertDialogContent>
    </AlertDialog>
  );
}
