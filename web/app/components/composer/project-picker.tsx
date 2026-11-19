/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import {
  Combobox,
  ComboboxContent,
  ComboboxItem,
  ComboboxList,
  ComboboxSearch,
  ComboboxStatus,
  ComboboxTrigger,
} from "@makeplane/propel/components/combobox";
import { Icon } from "@makeplane/propel/components/icon";
import { FolderInput, Plus } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "@prismoid/blocks/toast";
import { toastError, api, unwrap } from "~/lib/api/client";

export type ProjectRef = { id: string; name: string };

const PAGE = 30;
// The two rows that are not projects, as combobox values (a project id is a uuid, never these).
const NONE = "__none__";
const CREATE = "__create__";

/**
 * The project picker, built for hundreds of projects: server-side name search, newest first,
 * keyset pages loaded as the list scrolls, and creation folded into the same field - type a
 * name and create it when nothing matches. A task lives in exactly one project.
 */
export function ProjectPicker({
  value,
  onChange,
}: {
  value: ProjectRef | null;
  onChange: (project: ProjectRef | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [items, setItems] = useState<ProjectRef[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const seq = useRef(0); // stale-response guard: only the latest request may write state

  const fetchPage = useCallback(async (q: string, cursor: string | null) => {
    const mine = ++seq.current;
    setLoading(true);
    try {
      const page = await unwrap(
        api.GET("/v1/projects", {
          params: {
            query: {
              ...(q ? { q } : {}),
              ...(cursor ? { cursor } : {}),
              limit: PAGE,
            },
          },
        }),
      );
      if (seq.current !== mine) return;
      const rows = page.items.map((s) => ({ id: s.id, name: s.name }));
      setItems((cur) => (cursor ? [...cur, ...rows] : rows));
      setNextCursor(page.next_cursor ?? null);
    } catch {
      if (seq.current === mine) toast.error("Couldn't load projects.");
    } finally {
      if (seq.current === mine) setLoading(false);
    }
  }, []);

  // Open → fresh first page; typing → debounced server search.
  useEffect(() => {
    if (!open) return;
    const t = setTimeout(() => void fetchPage(query.trim(), null), query ? 250 : 0);
    return () => clearTimeout(t);
  }, [open, query, fetchPage]);

  // The next page loads when the sentinel scrolls into view - no buttons, no offsets.
  const sentinel = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = sentinel.current;
    if (!open || !nextCursor || !el) return;
    const io = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting) && !loading) {
        void fetchPage(query.trim(), nextCursor);
      }
    });
    io.observe(el);
    return () => io.disconnect();
  }, [open, nextCursor, loading, query, fetchPage]);

  const trimmed = query.trim();
  const exactMatch = items.some((s) => s.name.toLowerCase() === trimmed.toLowerCase());

  async function create() {
    if (!trimmed) return;
    try {
      const s = await unwrap(api.POST("/v1/projects", { body: { name: trimmed } }));
      onChange({ id: s.id, name: s.name });
      setOpen(false);
      setQuery("");
    } catch (e) {
      toastError(e, "Couldn't create the project.");
    }
  }

  function pick(project: ProjectRef | null) {
    onChange(project);
    setOpen(false);
    setQuery("");
  }

  function select(next: string | null) {
    if (next === CREATE) void create();
    else if (next === NONE) pick(null);
    else {
      const project = items.find((s) => s.id === next);
      if (project) pick(project);
    }
  }

  return (
    // Non-modal, as the kit's popover was. filter={null}: the server searches (name ILIKE,
    // newest first) - the combobox only renders and navigates. autoHighlight: typing then Enter
    // takes the first row, as cmdk did.
    <Combobox
      modal={false}
      filter={null}
      autoHighlight
      open={open}
      onOpenChange={(o) => {
        setOpen(o);
        if (!o) setQuery("");
      }}
      inputValue={query}
      onInputValueChange={setQuery}
      // Unset is null, not the "No project" row's value: Propel's trigger then shows its
      // placeholder in its own placeholder tone.
      value={value?.id ?? null}
      onValueChange={select}
    >
      {/* A destination, not a setting. Propel's trigger always reserves a value slot, so the
          unset state fills it with the placeholder "Project" - an empty slot read as a blank
          gap between the icon and the chevron. The chosen project's name replaces it. */}
      <ComboboxTrigger
        size="md"
        variant="ghost"
        width="fit"
        icon={<Icon icon={FolderInput} />}
        placeholder="Project"
        aria-label={value ? `Project: ${value.name}` : "Project: where this task will live"}
        title="Project: where this task will live"
      >
        {value ? <span className="block max-w-28 truncate">{value.name}</span> : undefined}
      </ComboboxTrigger>
      <ComboboxContent
        align="start"
        sizing="auto-fixed"
        aria-label="Project"
        search={<ComboboxSearch placeholder="Search or create a project…" />}
      >
        <ComboboxList>
          {!trimmed && <ComboboxItem value={NONE} label="No project" />}
          {items.map((s) => (
            <ComboboxItem key={s.id} value={s.id} label={s.name} />
          ))}
          {nextCursor && <div ref={sentinel} className="h-6" />}
          {trimmed && !exactMatch && !loading && (
            <ComboboxItem
              value={CREATE}
              label={`Create “${trimmed}”`}
              icon={<Icon icon={Plus} />}
            />
          )}
        </ComboboxList>
        <ComboboxStatus>
          {!loading && items.length === 0 && !trimmed
            ? "No projects yet - type a name to create one."
            : null}
        </ComboboxStatus>
      </ComboboxContent>
    </Combobox>
  );
}
