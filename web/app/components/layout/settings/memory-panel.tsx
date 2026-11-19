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
import { Field, FieldLabel } from "@makeplane/propel/components/field";
import { InputField } from "@makeplane/propel/components/input-field";
import { Brain, Plus, X } from "lucide-react";
import { useCallback, useState } from "react";
import { Link } from "react-router";
import {
  EmptyState,
  LoadingState,
  LoadMore,
  PageHeader,
  Rows,
  SearchBox,
  ToggleChip,
} from "~/components/kit";
import {
  MEMORY_KINDS,
  MemoryRowItem,
  type MemoryKind,
  type MemoryRow,
} from "~/components/memory/memory-row";
import { toastError, api, unwrap } from "~/lib/api/client";
import { ago } from "~/lib/format";
import { taskHref } from "~/lib/links";
import { useDebouncedValue, useKeysetList } from "~/lib/paging";

// ── Memory: the user's private half of the context ledger ────────────────────────────────────

const MEMORY_PAGE = 30;

/** The provenance line for a user-scope memory - module-level so every row shares one
 *  reference and React.memo holds. */
export const userMemoryMeta = (m: MemoryRow) => (
  <>
    {m.source_task_id ? (
      <>
        from{" "}
        <Link
          to={taskHref({ id: m.source_task_id, project_id: m.project_id })}
          className="hover:text-foreground underline underline-offset-2"
        >
          a task
        </Link>
      </>
    ) : (
      "added by you"
    )}
    {" · "}
    {ago(m.created_at)}
    {m.use_count > 0 && ` · used ${m.use_count} ${m.use_count === 1 ? "time" : "times"}`}
  </>
);

/** What the machine remembers about you - every row editable, retirable, and traceable to the
 *  task that minted it. Private: nothing here is visible to anyone else. */
export function MemoryPanel() {
  const [q, setQ] = useState("");
  const dq = useDebouncedValue(q.trim(), 250);
  const { items, loadMore, loadingMore, hasMore, mutate } = useKeysetList<MemoryRow>({
    fetchPage: (cursor) =>
      unwrap(
        api.GET("/v1/memories", {
          params: {
            query: {
              scope: "user",
              limit: MEMORY_PAGE,
              ...(dq ? { q: dq } : {}),
              ...(cursor ? { cursor } : {}),
            },
          },
        }),
      ),
    deps: [dq],
    orderKey: (m) => m.created_at,
  });

  // Mutations land locally from their responses - no full-list refetch per edit.
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
    <div className="space-y-6">
      <PageHeader
        as="h2"
        title="Memory"
        description="Short private notes Prismoid keeps for you: minted when a task delivers, or added by hand. Every task you run carries them as its “Your memory” context."
        actions={<AddMemory onAdded={onAdded} />}
      />
      <SearchBox value={q} onChange={setQ} placeholder="Search memories…" />
      {items === null ? (
        <LoadingState />
      ) : items.length === 0 ? (
        <EmptyState
          icon={Brain}
          title={dq ? `No matches for “${dq}”` : "Nothing remembered yet"}
          description={
            dq ? undefined : "Memories appear here as tasks finish, or add one yourself."
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
                meta={userMemoryMeta}
              />
            ))}
          </Rows>
          <LoadMore hasMore={hasMore} busy={loadingMore} onClick={() => void loadMore()} />
        </>
      )}
    </div>
  );
}

function AddMemory({ onAdded }: { onAdded: (created: MemoryRow) => void }) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [kind, setKind] = useState<MemoryKind>("note");
  const [busy, setBusy] = useState(false);

  async function add() {
    if (!text.trim()) return;
    setBusy(true);
    try {
      const created = await unwrap(
        api.POST("/v1/memories", {
          body: { scope: "user", kind, text: text.trim() },
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
            variant="primary"
            size="sm"
            stretch="auto"
            icon={<Icon icon={Plus} />}
            label="Add memory"
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
        <DialogMain>
          <DialogHeader>
            <DialogHeading>
              <DialogTitle>Add a memory</DialogTitle>
              <DialogDescription>
                One durable sentence: a preference, a fact, a standing instruction. It shapes every
                future task you run.
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
                placeholder="Prefers concise memos with numbers tables."
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
            onClick={() => void add()}
            disabled={busy || !text.trim()}
            label="Add memory"
          />
        </DialogActions>
      </DialogContent>
    </Dialog>
  );
}
