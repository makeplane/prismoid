/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The Memory tab (ADR-056) - what this bot has learned, as rows you can read, edit, and
// retire. The MemoryPanel anatomy verbatim (search → rows → load more), riding the same
// `/v1/memories` resource with the bot lane's params. Permission truth lives server-side (the
// page's standing convention): mutations are offered and a 403/404 answers honestly.

import { Brain, Plus, X } from "lucide-react";
import { useCallback, useState } from "react";
import { Link } from "react-router";
import { EmptyState, LoadingState, LoadMore, Rows, SearchBox, ToggleChip } from "~/components/kit";
import {
  MEMORY_KINDS,
  MemoryRowItem,
  type MemoryKind,
  type MemoryRow,
} from "~/components/memory/memory-row";
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
import { Field, FieldLabel } from "@makeplane/propel/components/field";
import { Icon } from "@makeplane/propel/components/icon";
import { IconButton } from "@makeplane/propel/components/icon-button";
import { InputField } from "@makeplane/propel/components/input-field";
import { api, toastError, unwrap } from "~/lib/api/client";
import { ago } from "~/lib/format";
import { taskHref } from "~/lib/links";
import { useDebouncedValue, useKeysetList } from "~/lib/paging";

const PAGE = 30;

/** Provenance for a bot memory - module-level so React.memo holds on every row. */
export const botMemoryMeta = (m: MemoryRow) => (
  <>
    {m.source_task_id ? (
      <>
        learned from{" "}
        <Link
          to={taskHref({ id: m.source_task_id, project_id: m.project_id })}
          className="hover:text-foreground underline underline-offset-2"
        >
          a task
        </Link>
      </>
    ) : (
      "added by hand"
    )}
    {" · "}
    {ago(m.created_at)}
    {m.use_count > 0 && ` · used ${m.use_count} ${m.use_count === 1 ? "time" : "times"}`}
  </>
);

export function MemoryTab({ name, projectId }: { name: string; projectId: string | null }) {
  const [q, setQ] = useState("");
  const dq = useDebouncedValue(q.trim(), 250);
  const { items, loadMore, loadingMore, hasMore, mutate } = useKeysetList<MemoryRow>({
    fetchPage: (cursor) =>
      unwrap(
        api.GET("/v1/memories", {
          params: {
            query: {
              scope: "bot",
              bot: name,
              ...(projectId ? { project_id: projectId } : {}),
              limit: PAGE,
              ...(dq ? { q: dq } : {}),
              ...(cursor ? { cursor } : {}),
            },
          },
        }),
      ),
    deps: [dq, name, projectId],
    orderKey: (m) => m.created_at,
  });

  const onAdded = useCallback((created: MemoryRow) => mutate((cur) => [created, ...cur]), [mutate]);
  const onEdited = useCallback(
    (next: MemoryRow) => mutate((cur) => cur.map((m) => (m.id === next.id ? next : m))),
    [mutate],
  );
  const onRetired = useCallback(
    (id: string) => mutate((cur) => cur.filter((m) => m.id !== id)),
    [mutate],
  );

  return (
    <div className="scrollbar-thin min-h-0 flex-1 space-y-4 overflow-y-auto px-6 py-5">
      <div className="flex items-center gap-2">
        <SearchBox value={q} onChange={setQ} placeholder="Search memories…" className="flex-1" />
        <AddBotMemory name={name} projectId={projectId} onAdded={onAdded} />
      </div>
      {items === null ? (
        <LoadingState />
      ) : items.length === 0 ? (
        <EmptyState
          icon={Brain}
          title={dq ? `No matches for “${dq}”` : "Nothing learned yet"}
          description={
            dq
              ? undefined
              : "Standing facts and preferences land here - from chats with this bot, or added by hand. Every direct task carries them."
          }
        />
      ) : (
        <>
          <Rows as="ul">
            {items.map((m) => (
              <MemoryRowItem
                key={m.id}
                memory={m}
                canMutate
                onEdited={onEdited}
                onRetired={onRetired}
                meta={botMemoryMeta}
              />
            ))}
          </Rows>
          <LoadMore hasMore={hasMore} busy={loadingMore} onClick={() => void loadMore()} />
        </>
      )}
    </div>
  );
}

function AddBotMemory({
  name,
  projectId,
  onAdded,
}: {
  name: string;
  projectId: string | null;
  onAdded: (created: MemoryRow) => void;
}) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [kind, setKind] = useState<MemoryKind>("note");
  const [busy, setBusy] = useState(false);

  async function add() {
    if (!text.trim() || busy) return; // Enter twice is one memory, not two
    setBusy(true);
    try {
      const created = await unwrap(
        api.POST("/v1/memories", {
          body: {
            scope: "bot",
            bot_name: name,
            ...(projectId ? { project_id: projectId } : {}),
            kind,
            text: text.trim(),
          },
        }),
      );
      setOpen(false);
      setText("");
      onAdded(created);
    } catch (e) {
      toastError(e, "Couldn't add the memory.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger
        render={
          <Button
            variant="secondary"
            size="sm"
            stretch="auto"
            icon={<Icon icon={Plus} />}
            label="Add memory"
          />
        }
      />
      <DialogContent size="xs">
        <DialogMain>
          <DialogHeader>
            <DialogHeading>
              <DialogTitle>Teach {name} something</DialogTitle>
              <DialogDescription>
                One durable sentence: a fact, a reference, a standing preference. It rides every
                direct task this bot runs - and it is never part of the published bot.
              </DialogDescription>
            </DialogHeading>
          </DialogHeader>
          <DialogBody>
            <div className="space-y-3">
              <InputField
                size="lg"
                orientation="vertical"
                label="Memory"
                value={text}
                onChange={(e) => setText(e.currentTarget.value)}
                placeholder="The Q3 workbook lives in the finance project."
                maxLength={500}
                autoFocus
                onKeyDown={(e) => {
                  if (e.key === "Enter") void add();
                }}
              />
              <Field size="lg">
                <FieldLabel size="lg" inset={false}>
                  Kind
                </FieldLabel>
                <div className="flex flex-wrap gap-2">
                  {MEMORY_KINDS.map((k) => (
                    <ToggleChip key={k} active={kind === k} onClick={() => setKind(k)}>
                      <span className="capitalize">{k}</span>
                    </ToggleChip>
                  ))}
                </div>
              </Field>
            </div>
          </DialogBody>
        </DialogMain>
        <DialogActions>
          <Button
            variant="primary"
            size="md"
            stretch="auto"
            label="Add memory"
            onClick={() => void add()}
            disabled={busy || !text.trim()}
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
