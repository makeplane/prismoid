/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// "+ Custom connector" - the by-URL door for anything the catalog does not offer. MOVED here
// from the settings panel when /connectors became the one home; the dialog body is
// the original dialog, relabeled.

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
import { Plug, Plus, X } from "lucide-react";
import { useState } from "react";
import { toast } from "@prismoid/blocks/toast";
import { api, toastError, unwrap } from "~/lib/api/client";
import type { ConnectorRow } from "~/components/connectors/vocabulary";

export function AddConnector({ onAdded }: { onAdded: (created: ConnectorRow) => void }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);

  async function create() {
    if (!url.trim()) return;
    setBusy(true);
    try {
      const row = await unwrap(
        api.POST("/v1/connectors", {
          body: {
            server_url: url.trim(),
            ...(name.trim() ? { name: name.trim() } : {}),
          },
        }),
      );
      onAdded(row);
      close();
      if (row.status !== "live") toast("Added. Now connect it to authorize.");
    } catch (e) {
      toastError(e, "Couldn't add the connector.");
    } finally {
      setBusy(false);
    }
  }

  function close() {
    setOpen(false);
    setName("");
    setUrl("");
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
            label="Custom connector"
          />
        }
      />
      <DialogContent size="xs">
        <DialogMain>
          <DialogHeader>
            <DialogHeading>
              <DialogTitle>Add a custom connector</DialogTitle>
              <DialogDescription>
                The MCP endpoint of the service. Servers that need sign-in park until you hit
                Connect.
              </DialogDescription>
            </DialogHeading>
          </DialogHeader>
          <DialogBody>
            <div className="space-y-4">
              <InputField
                size="lg"
                orientation="vertical"
                label="Server URL"
                value={url}
                onChange={(e) => setUrl(e.currentTarget.value)}
                placeholder="https://mcp.example.com/mcp"
                autoFocus
              />
              <InputField
                size="lg"
                orientation="vertical"
                label="Name (optional)"
                value={name}
                onChange={(e) => setName(e.currentTarget.value)}
                placeholder="My server"
              />
            </div>
          </DialogBody>
        </DialogMain>
        <DialogActions>
          <Button
            variant="primary"
            size="md"
            stretch="auto"
            onClick={() => void create()}
            disabled={busy || !url.trim()}
            icon={<Icon icon={Plug} />}
            label="Add"
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
