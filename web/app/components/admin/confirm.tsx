/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The console's two confirm shapes, in one place.
//
// Every operator verb is guarded, and there are exactly two guards: a plain confirm, and a
// confirm that takes PROSE - because suspension and cancellation both put their reason in the
// audit row, and a reason is the substance of that row, not decoration. These lived as three
// near-identical local dialogs (two of them the same dialog with different copy) across the
// user detail page and the task peek.
//
// Both close only on success: an error toasts and leaves the dialog standing with the typed
// reason intact, because retyping a paragraph after a network blip is its own small insult.

import {
  AlertDialog,
  AlertDialogActions,
  AlertDialogBody,
  AlertDialogClose,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogHeader,
  AlertDialogIntro,
  AlertDialogTitle,
  AlertDialogTrigger,
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
  DialogTrigger,
} from "@makeplane/propel/components/dialog";
import { Icon } from "@makeplane/propel/components/icon";
import { IconButton } from "@makeplane/propel/components/icon-button";
import { TextAreaField } from "@makeplane/propel/components/text-area-field";
import { X } from "lucide-react";
import { useState, type ReactElement, type ReactNode } from "react";
import { toastError } from "~/lib/api/client";

/** One confirm-dialog verb - open, read, confirm, done. */
export function ConfirmAction({
  trigger,
  title,
  description,
  destructive,
  onConfirm,
}: {
  trigger: string;
  title: string;
  description: string;
  destructive?: boolean;
  onConfirm: () => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);

  async function run() {
    setBusy(true);
    try {
      await onConfirm();
      setOpen(false);
    } catch (e) {
      toastError(e, "That didn't go through.");
    } finally {
      setBusy(false);
    }
  }

  // A pure yes/no, so Propel's alert dialog: Cancel or Escape backs out, an outside press does not.
  return (
    <AlertDialog open={open} onOpenChange={setOpen}>
      <AlertDialogTrigger
        render={
          <Button
            variant={destructive ? "danger-outline" : "secondary"}
            size="sm"
            stretch="auto"
            label={trigger}
          />
        }
      />
      <AlertDialogContent>
        <AlertDialogBody>
          <AlertDialogHeader>
            <AlertDialogIntro>
              <AlertDialogTitle>{title}</AlertDialogTitle>
              <AlertDialogDescription>{description}</AlertDialogDescription>
            </AlertDialogIntro>
          </AlertDialogHeader>
        </AlertDialogBody>
        <AlertDialogActions>
          <Button
            variant="secondary"
            size="md"
            stretch="auto"
            label="Cancel"
            render={<AlertDialogClose />}
          />
          <Button
            variant={destructive ? "danger" : "primary"}
            size="md"
            stretch="auto"
            label={trigger}
            disabled={busy}
            onClick={() => void run()}
          />
        </AlertDialogActions>
      </AlertDialogContent>
    </AlertDialog>
  );
}

/**
 * A verb that takes prose. The reason is REQUIRED - the confirm stays disabled until something
 * is typed - because the audit row exists to answer "why", and an empty why is a row that
 * cannot answer the person it was written about.
 */
export function ReasonDialog({
  trigger,
  title,
  description,
  placeholder,
  confirmLabel,
  minLength = 1,
  onConfirm,
}: {
  /** A label for the stock danger-outline trigger, or the trigger element itself. */
  trigger: string | ReactElement;
  title: ReactNode;
  description: ReactNode;
  placeholder: string;
  confirmLabel: string;
  /** The shortest reason the door accepts (its serializer's min_length): the confirm waits for it. */
  minLength?: number;
  onConfirm: (reason: string) => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const ready = reason.trim().length >= minLength;

  async function run() {
    if (!ready) return;
    setBusy(true);
    try {
      await onConfirm(reason.trim());
      setOpen(false);
      setReason("");
    } catch (e) {
      toastError(e, "That didn't go through.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger
        render={
          typeof trigger === "string" ? (
            <Button variant="danger-outline" size="sm" stretch="auto" label={trigger} />
          ) : (
            trigger
          )
        }
      />
      <DialogContent size="xs">
        <DialogCloseGroup>
          <IconButton
            variant="ghost"
            size="xs"
            aria-label="Close"
            icon={<Icon icon={X} />}
            render={<DialogClose />}
          />
        </DialogCloseGroup>
        <DialogMain>
          <DialogHeader>
            <DialogHeading>
              <DialogTitle>{title}</DialogTitle>
              <DialogDescription>{description}</DialogDescription>
            </DialogHeading>
          </DialogHeader>
          <DialogBody>
            <TextAreaField
              size="lg"
              label="Reason"
              value={reason}
              onChange={(e) => setReason(e.currentTarget.value)}
              placeholder={placeholder}
              hint={minLength > 1 ? `At least ${minLength} characters.` : undefined}
              autoResize
              autoFocus
            />
          </DialogBody>
        </DialogMain>
        <DialogActions>
          <Button
            variant="danger"
            size="md"
            stretch="auto"
            label={confirmLabel}
            disabled={busy || !ready}
            onClick={() => void run()}
          />
        </DialogActions>
      </DialogContent>
    </Dialog>
  );
}
