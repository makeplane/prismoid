/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

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
import { InputField } from "@makeplane/propel/components/input-field";
import { Check, Copy, KeyRound, Plus, Trash2, X } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { toast } from "@prismoid/blocks/toast";
import { CopyButton, EmptyState, LoadingState, PageHeader, Rows, Section } from "~/components/kit";
import { Card, CardContent } from "@prismoid/blocks/card";
import { toastError, api, API_PROTOCOL, API_URL, unwrap } from "~/lib/api/client";
import { ago } from "~/lib/format";
import { cn } from "~/lib/utils";

// ── API keys: keys for the CLI and direct API access, plus connection info ────────────────────

const fetchKeys = () => unwrap(api.GET("/v1/auth/api-keys"));

export function ApiKeysPanel() {
  const [keys, setKeys] = useState<Awaited<ReturnType<typeof fetchKeys>> | null>(null);
  const load = useCallback(() => {
    fetchKeys()
      .then(setKeys)
      .catch(() => toast.error("Couldn't load API keys."));
  }, []);
  useEffect(load, [load]);

  async function revoke(id: string) {
    await unwrap(
      api.DELETE("/v1/auth/api-keys/{key_id}", {
        params: { path: { key_id: id } },
      }),
    ).catch((e) => toastError(e, "Couldn't revoke."));
    load();
  }

  return (
    <div className="space-y-6">
      <PageHeader as="h2" title="API keys" description="Keys for the CLI and direct API access." />

      <Section title="Keys" actions={<CreateKey onCreated={load} />}>
        {keys === null ? (
          <LoadingState />
        ) : keys.length === 0 ? (
          <EmptyState
            compact
            title="No API keys yet"
            description="Create one to use the CLI or call the API directly."
          />
        ) : (
          <Rows as="ul">
            {keys.map((k) => (
              <li key={k.id} className="flex items-center gap-3 px-4 py-3 text-body-sm-regular">
                <KeyRound className="text-muted-foreground size-4 shrink-0" />
                <div className="min-w-0 flex-1">
                  <p
                    className={cn(
                      "font-medium",
                      k.revoked_at && "text-muted-foreground line-through",
                    )}
                  >
                    {k.name}
                  </p>
                  <p className="text-muted-foreground text-caption-md-regular">
                    created {ago(k.created_at)}
                    {k.last_used_at ? ` · last used ${ago(k.last_used_at)}` : " · never used"}
                  </p>
                </div>
                {k.revoked_at ? (
                  <span className="text-muted-foreground text-caption-md-regular">revoked</span>
                ) : (
                  <Button
                    variant="danger-outline"
                    size="sm"
                    stretch="auto"
                    onClick={() => void revoke(k.id)}
                    icon={<Icon icon={Trash2} />}
                    label="Revoke"
                  />
                )}
              </li>
            ))}
          </Rows>
        )}
      </Section>

      <Section title="Connection">
        <Card size="sm">
          <CardContent className="text-muted-foreground space-y-1 text-body-sm-regular">
            <p>
              API endpoint{" "}
              <code className="bg-muted text-foreground rounded-sm px-1.5 py-0.5">{API_URL}</code>
            </p>
            <p>
              Protocol{" "}
              <code className="bg-muted text-foreground rounded-sm px-1.5 py-0.5">
                {API_PROTOCOL}
              </code>
            </p>
          </CardContent>
        </Card>
      </Section>
    </div>
  );
}

function CreateKey({ onCreated }: { onCreated: () => void }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [created, setCreated] = useState<{ key: string } | null>(null);

  async function create() {
    if (!name.trim()) return;
    setBusy(true);
    try {
      const res = await unwrap(api.POST("/v1/auth/api-keys", { body: { name: name.trim() } }));
      setCreated({ key: res.key });
      onCreated();
    } catch (e) {
      toastError(e, "Couldn't create the key.");
    } finally {
      setBusy(false);
    }
  }

  function close() {
    setOpen(false);
    setName("");
    setCreated(null);
  }

  return (
    <Dialog open={open} onOpenChange={(o) => (o ? setOpen(true) : close())}>
      <DialogTrigger
        render={
          <Button
            variant="primary"
            size="sm"
            stretch="auto"
            icon={<Icon icon={Plus} />}
            label="New key"
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
        {created ? (
          <>
            <DialogMain>
              <DialogHeader>
                <DialogHeading>
                  <DialogTitle>Copy your key now</DialogTitle>
                  <DialogDescription>
                    This is the only time you'll see it. Store it somewhere safe.
                  </DialogDescription>
                </DialogHeading>
              </DialogHeader>
              <DialogBody>
                <div className="flex items-center gap-2 rounded-md border p-2">
                  <Check className="text-success size-4 shrink-0" />
                  <code className="min-w-0 flex-1 truncate font-mono text-caption-md-regular">
                    {created.key}
                  </code>
                  <CopyButton value={created.key} />
                </div>
              </DialogBody>
            </DialogMain>
            <DialogActions>
              <Button variant="primary" size="md" stretch="auto" onClick={close} label="Done" />
            </DialogActions>
          </>
        ) : (
          <>
            <DialogMain>
              <DialogHeader>
                <DialogHeading>
                  <DialogTitle>New API key</DialogTitle>
                  <DialogDescription>Name it so you remember where it's used.</DialogDescription>
                </DialogHeading>
              </DialogHeader>
              <DialogBody>
                <InputField
                  size="lg"
                  orientation="vertical"
                  label="Name"
                  value={name}
                  onChange={(e) => setName(e.currentTarget.value)}
                  placeholder="CLI on my laptop"
                  autoFocus
                />
              </DialogBody>
            </DialogMain>
            <DialogActions>
              <Button
                variant="primary"
                size="md"
                stretch="auto"
                onClick={() => void create()}
                disabled={busy || !name.trim()}
                icon={<Icon icon={Copy} />}
                label="Create key"
              />
            </DialogActions>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
