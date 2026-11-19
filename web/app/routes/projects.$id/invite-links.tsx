/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The owner's Invite links (ADR-062), a Section of the Project's People. A link is a
// bearer code the owner mints (role, expiry) and hands to one person: whoever in the Project's
// workspace opens it first joins at that role, and then it is dead - every link is single-use
// and time-bound. The full link exists in the browser exactly once,
// in the dialog that made it - the list afterwards carries no code, only each link's state, and
// a live one can be revoked.

import { capitalize } from "~/lib/utils";
import { Link2, Trash2, X } from "lucide-react";
import { useEffect, useReducer, useState } from "react";
import { CopyButton, Pill, Rows, Section } from "~/components/kit";
import type { Tone } from "~/components/kit";
import { Badge } from "@makeplane/propel/components/badge";
import { Button } from "@makeplane/propel/components/button";
import {
  Dialog,
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
import { Input, InputGroup } from "@makeplane/propel/components/input";
import { SelectField } from "@makeplane/propel/components/select-field";
import { api, toastError, unwrap } from "~/lib/api/client";
import type { components } from "~/lib/api/types.gen";
import { ago, inAgo } from "~/lib/format";

type InviteOut = components["schemas"]["ProjectInviteOut"];
type InviteRole = components["schemas"]["InviteRole"];

/** What the owner chooses: the role the link grants and how long it lives. It always works once. */
export type LinkChoice = { role: InviteRole; days: number };

export const EXPIRY_DAYS = [1, 7, 30] as const;
/** The server's own default expiry (7 days), with the role the Share popover mints. */
export const DEFAULT_CHOICE: LinkChoice = { role: "editor", days: 7 };

/** Mint a link and return the URL a person opens. The code rides back once and only here. */
export async function createInviteLink(
  projectId: string,
  choice: LinkChoice,
  origin: string,
): Promise<string> {
  const made = await unwrap(
    api.POST("/v1/projects/{project_id}/invites", {
      params: { path: { project_id: projectId } },
      body: { role: choice.role, expires_in_days: choice.days },
    }),
  );
  return `${origin}/join/${made.code}`;
}

export async function revokeInviteLink(projectId: string, inviteId: string): Promise<void> {
  await unwrap(
    api.DELETE("/v1/projects/{project_id}/invites/{project_invite_id}", {
      params: { path: { project_id: projectId, project_invite_id: inviteId } },
    }),
  );
}

const STATE: Record<InviteOut["state"], { label: string; tone: Tone }> = {
  live: { label: "Live", tone: "success" },
  expired: { label: "Expired", tone: "muted" },
  used_up: { label: "Used", tone: "muted" },
  revoked: { label: "Revoked", tone: "muted" },
};

/** Days left round UP, so a link minted for 7 days reads "in 7d" from its first minute - `inAgo`
 *  floors, and "in 6d" on a fresh 7-day link would look like the wrong setting was sent. */
function expiresIn(iso: string): string {
  const hours = (Date.parse(iso) - Date.now()) / 3_600_000;
  return hours >= 24 ? `in ${Math.ceil(hours / 24)}d` : inAgo(iso);
}

/** "expires in 7d" while it lives, "expired 2d ago" once it lapsed; a used or revoked link's
 *  own state already says why it is dead, so it carries no date. */
function expiry(l: InviteOut): string | null {
  if (l.state === "live") return `expires ${expiresIn(l.expires_at)}`;
  if (l.state === "expired") return `expired ${ago(l.expires_at)}`;
  return null;
}

/** The list's body as a pure view: `null` is still loading (nothing yet, no flash of "none"),
 *  unless the load `failed` - then it says so and offers a retry, because "No invite links yet"
 *  over a list that never loaded is a lie the owner would act on (by minting a duplicate). */
export function InviteLinkRows({
  links,
  failed,
  onRevoke,
  onRetry,
}: {
  links: readonly InviteOut[] | null;
  failed: boolean;
  onRevoke: (inviteId: string) => void;
  onRetry: () => void;
}) {
  if (links === null) {
    if (!failed) return null;
    return (
      <div className="flex items-center gap-3">
        <p className="text-muted-foreground text-caption-md-regular">
          Couldn't load the invite links.
        </p>
        <Button variant="secondary" size="sm" stretch="auto" label="Retry" onClick={onRetry} />
      </div>
    );
  }
  if (links.length === 0) {
    return (
      <p className="text-muted-foreground text-caption-md-regular">
        No invite links yet. Each link lets one person in your workspace join this Project at its
        role.
      </p>
    );
  }
  return (
    <Rows as="ul">
      {links.map((l) => (
        <li key={l.id} className="flex items-center gap-3 px-4 py-3">
          <Badge variant="neutral" size="xs" label={capitalize(l.role)} />
          <span className="text-muted-foreground min-w-0 flex-1 truncate text-caption-md-regular">
            {expiry(l)}
          </span>
          <Pill tone={STATE[l.state].tone} className="shrink-0">
            {STATE[l.state].label}
          </Pill>
          {l.state === "live" ? (
            <IconButton
              variant="ghost"
              size="sm"
              icon={<Icon icon={Trash2} />}
              aria-label={`Revoke ${l.role} link`}
              onClick={() => onRevoke(l.id)}
            />
          ) : (
            // keeps the columns of live and dead rows aligned
            <span className="size-7 shrink-0" aria-hidden />
          )}
        </li>
      ))}
    </Rows>
  );
}

/** The made link: read-only so it selects and copies, and said to be shown once. */
export function CreatedLink({ url }: { url: string }) {
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-1">
        <InputGroup size="md">
          <Input
            size="md"
            readOnly
            value={url}
            aria-label="Invite link"
            onFocus={(e) => e.currentTarget.select()}
          />
        </InputGroup>
        <CopyButton value={url} label="Copy invite link" />
      </div>
      <p className="text-muted-foreground text-caption-md-regular">This link is shown once.</p>
    </div>
  );
}

const ROLE_LABEL: Record<InviteRole, string> = {
  editor: "Editor - create, run, resolve",
  viewer: "Viewer - read only",
};
const days = (n: number) => (n === 1 ? "1 day" : `${n} days`);

/** The dialog's body: the two choices, or - once made - the link, shown once. */
export function NewLinkBody({
  choice,
  onChoice,
  url,
  busy,
  onCreate,
}: {
  choice: LinkChoice;
  onChoice: (next: LinkChoice) => void;
  url: string | null;
  busy: boolean;
  onCreate: () => void;
}) {
  if (url) return <CreatedLink url={url} />;
  return (
    <div className="space-y-3">
      <SelectField
        modal={false}
        size="lg"
        variant="secondary"
        width="auto"
        label="Role"
        value={choice.role}
        onValueChange={(v) => v && onChoice({ ...choice, role: v as InviteRole })}
        options={[
          { value: "editor", label: ROLE_LABEL.editor },
          { value: "viewer", label: ROLE_LABEL.viewer },
        ]}
      />
      <SelectField
        modal={false}
        size="lg"
        variant="secondary"
        width="auto"
        label="Expires"
        value={String(choice.days)}
        onValueChange={(v) => v && onChoice({ ...choice, days: Number(v) })}
        options={EXPIRY_DAYS.map((n) => ({ value: String(n), label: days(n) }))}
      />
      <p className="text-muted-foreground text-caption-md-regular">
        One person can join with it. It stops working after that, or when it expires.
      </p>
      <div className="flex justify-end">
        <Button
          variant="primary"
          size="md"
          stretch="auto"
          label="Create link"
          disabled={busy}
          onClick={onCreate}
        />
      </div>
    </div>
  );
}

/** The create dialog's whole state, as one pure machine: whether it is open, what is picked, and
 *  the link it made - which lives only as long as the dialog that made it. `gen` names a dialog's
 *  life (bumped on every open and close), so a create that resolves after its dialog was closed
 *  (or closed and reopened) is recognised as stale and cannot bring a dead link back on screen. */
export type LinkDialog = {
  open: boolean;
  choice: LinkChoice;
  url: string | null;
  gen: number;
};
export type LinkDialogAction =
  | { type: "open" }
  | { type: "close" }
  | { type: "choose"; choice: LinkChoice }
  | { type: "made"; gen: number; url: string };

export const CLOSED_DIALOG: LinkDialog = {
  open: false,
  choice: DEFAULT_CHOICE,
  url: null,
  gen: 0,
};

export function linkDialogReducer(state: LinkDialog, action: LinkDialogAction): LinkDialog {
  switch (action.type) {
    case "open": // every opening starts from the defaults, with no link
      return { open: true, choice: DEFAULT_CHOICE, url: null, gen: state.gen + 1 };
    case "close": // the code is gone with the dialog
      return { open: false, choice: DEFAULT_CHOICE, url: null, gen: state.gen + 1 };
    case "choose":
      return state.open ? { ...state, choice: action.choice } : state;
    case "made":
      return state.open && state.gen === action.gen ? { ...state, url: action.url } : state;
  }
}

/** The Project's Invite links, for its owner: the list, a Create link dialog, revoke. */
export function InviteLinks({ projectId }: { projectId: string }) {
  const [links, setLinks] = useState<InviteOut[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [tick, setTick] = useState(0); // bump to re-read the list after a change
  const [dialog, dispatch] = useReducer(linkDialogReducer, CLOSED_DIALOG);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setFailed(false);
    unwrap(
      api.GET("/v1/projects/{project_id}/invites", {
        params: { path: { project_id: projectId } },
      }),
    ).then(
      (rows) => !cancelled && setLinks(rows),
      // A refresh that fails keeps the list on screen; only a first load with nothing to show errs.
      () => !cancelled && setFailed(true),
    );
    return () => {
      cancelled = true;
    };
  }, [projectId, tick]);

  async function create() {
    if (busy) return;
    const gen = dialog.gen; // this dialog's life: a stale answer is recognised by it
    setBusy(true);
    try {
      const url = await createInviteLink(projectId, dialog.choice, window.location.origin);
      dispatch({ type: "made", gen, url });
      setTick((t) => t + 1); // the link exists either way: the list shows it
    } catch (e) {
      toastError(e, "Couldn't create the invite link.");
    } finally {
      setBusy(false);
    }
  }

  async function revoke(inviteId: string) {
    try {
      await revokeInviteLink(projectId, inviteId);
      setTick((t) => t + 1);
    } catch (e) {
      toastError(e, "Couldn't revoke the link.");
    }
  }

  return (
    <Section
      title="Invite links"
      actions={
        <Dialog
          open={dialog.open}
          onOpenChange={(next) => dispatch({ type: next ? "open" : "close" })}
        >
          <DialogTrigger
            render={
              <Button
                variant="secondary"
                size="sm"
                stretch="auto"
                icon={<Icon icon={Link2} />}
                label="Create link"
              />
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
            {/* No DialogActions: NewLinkBody carries its own Create button, and once the link
                is made the body is the link alone. */}
            <DialogMain>
              <DialogHeader>
                <DialogHeading>
                  <DialogTitle>Create an invite link</DialogTitle>
                  <DialogDescription>
                    The first person in your workspace who opens it joins this project at the role
                    you pick. Make one link per person.
                  </DialogDescription>
                </DialogHeading>
              </DialogHeader>
              <DialogBody>
                <NewLinkBody
                  choice={dialog.choice}
                  onChoice={(choice) => dispatch({ type: "choose", choice })}
                  url={dialog.url}
                  busy={busy}
                  onCreate={() => void create()}
                />
              </DialogBody>
            </DialogMain>
          </DialogContent>
        </Dialog>
      }
    >
      <InviteLinkRows
        links={links}
        failed={failed}
        onRevoke={(id) => void revoke(id)}
        onRetry={() => setTick((t) => t + 1)}
      />
    </Section>
  );
}
