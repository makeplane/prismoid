/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useLocation } from "react-router";
import { toast } from "@prismoid/blocks/toast";
import { toastError, api, ApiError, unwrap } from "~/lib/api/client";
import type { components } from "~/lib/api/types.gen";

type TaskSummary = components["schemas"]["TaskSummaryOut"];

export interface TaskWindowRow {
  id: string;
  label: string;
  status: string;
  /** The settled turn's typed ending (`TaskSummaryOut.ending`): an honest could-not wears its own mark. */
  ending?: string | null;
  pinned: boolean;
  createdAt: string;
  /** Who made it (`TaskSummaryOut.creator_id`): a teammate's Project task lists here too, and
   *  its rename, pin and delete are the creator's alone. */
  creatorId?: string | null;
  /** The task's Project (`TaskSummaryOut.project_id`): where its canonical URL lives. */
  projectId?: string | null;
}

/** The row verbs this caller may use - the server's rule, so the menu never offers a 403:
 *  rename, pin and delete are the creator's (a Project owner deletes from the task page, where
 *  the detail says `can_curate`). Unknown caller or creator offers nothing. Pure. */
export function rowVerbs(
  row: Pick<TaskWindowRow, "creatorId">,
  myId: string | null | undefined,
): { rename: boolean; pin: boolean; delete: boolean } {
  const mine = !!myId && row.creatorId === myId;
  return { rename: mine, pin: mine, delete: mine };
}

interface TaskWindowValue {
  pins: TaskWindowRow[];
  recents: TaskWindowRow[];
  latest: TaskWindowRow[];
  rename: (id: string, next: string) => Promise<void>;
  togglePin: (row: TaskWindowRow) => Promise<void>;
  remove: (id: string) => Promise<boolean>;
  /** Live surfaces push what they KNOW here (the task page streams status and the minted
   *  title) - without this, a delivered task's sidebar row spins until the next
   *  navigation refetch. Local-only; the wire already knows. */
  patch: (id: string, patch: Partial<TaskWindowRow>) => void;
}

const TaskWindowContext = createContext<TaskWindowValue | null>(null);
export const TASK_RECENT_WINDOW = 20;
const PIN_WINDOW = 50;

export const toTaskRow = (task: TaskSummary): TaskWindowRow => ({
  id: task.id,
  label: task.title || task.goal,
  status: task.status,
  ending: task.ending ?? null,
  pinned: task.pinned_at != null,
  createdAt: task.created_at,
  creatorId: task.creator_id,
  projectId: task.project_id,
});

/** Newest first, ids breaking timestamp ties - the same order the keyset walks. */
const byRecency = (a: TaskWindowRow, b: TaskWindowRow): number =>
  Date.parse(b.createdAt) - Date.parse(a.createdAt) || b.id.localeCompare(a.id);

/** Re-home a row into a group at its true position - a pin toggle moves a task between
 *  groups, it does not make the task newer. */
const insertByRecency = (rows: TaskWindowRow[], row: TaskWindowRow): TaskWindowRow[] =>
  [...rows, row].sort(byRecency);

/** One newest-first task window for every consumer. Pinned and unpinned queries are disjoint,
 *  but id-deduplication keeps an optimistic move or a racing refresh harmless. */
export function mergeTaskWindow(pins: TaskWindowRow[], recents: TaskWindowRow[]): TaskWindowRow[] {
  const byId = new Map<string, TaskWindowRow>();
  for (const row of [...pins, ...recents]) {
    if (!byId.has(row.id)) byId.set(row.id, row);
  }
  return [...byId.values()].sort(byRecency);
}

export interface TaskWindowSeed {
  pins: TaskWindowRow[];
  recents: TaskWindowRow[];
}

/** Shared navigation task data for the sidebar window and Home's five-row continuation
 *  point. The shell loader seeds it so the first paint carries rows (no pop-in); after
 *  that it refreshes on every navigation. */
export function TaskWindowProvider({
  initial,
  children,
}: {
  initial?: TaskWindowSeed;
  children: ReactNode;
}) {
  const { pathname } = useLocation();
  const [pins, setPins] = useState<TaskWindowRow[]>(initial?.pins ?? []);
  const [recents, setRecents] = useState<TaskWindowRow[]>(initial?.recents ?? []);
  const seeded = useRef(initial != null);

  useEffect(() => {
    if (seeded.current) {
      // The shell loader already fetched this exact window; skip the mount refetch.
      seeded.current = false;
      return;
    }
    let live = true;
    void Promise.all([
      unwrap(
        api.GET("/v1/tasks", {
          params: { query: { pinned: true, limit: PIN_WINDOW } },
        }),
      ),
      unwrap(
        api.GET("/v1/tasks", {
          params: { query: { pinned: false, limit: TASK_RECENT_WINDOW } },
        }),
      ),
    ])
      .then(([pinnedPage, recentPage]) => {
        if (!live) return;
        setPins(pinnedPage.items.map(toTaskRow));
        setRecents(recentPage.items.map(toTaskRow));
      })
      .catch(() => {
        // Navigation data is best-effort: preserve the last good window.
      });
    return () => {
      live = false;
    };
  }, [pathname]);

  const patchRow = useCallback((id: string, patch: Partial<TaskWindowRow>) => {
    const apply = (rows: TaskWindowRow[]) =>
      rows.map((row) => (row.id === id ? { ...row, ...patch } : row));
    setPins(apply);
    setRecents(apply);
  }, []);

  const rename = useCallback(
    async (id: string, next: string) => {
      const name = next.trim().slice(0, 60); // the wire's hard lip
      const previous = [...pins, ...recents].find((row) => row.id === id)?.label;
      if (!name || name === previous) return;
      patchRow(id, { label: name });
      try {
        await unwrap(
          api.PATCH("/v1/tasks/{task_id}", {
            params: { path: { task_id: id } },
            body: { title: name },
          }),
        );
      } catch (error) {
        if (previous) patchRow(id, { label: previous });
        toast.error(error instanceof ApiError ? error.message : "Couldn't rename the task.");
      }
    },
    [patchRow, pins, recents],
  );

  const togglePin = useCallback(async (row: TaskWindowRow) => {
    const next = !row.pinned;
    if (next) {
      setRecents((current) => current.filter((item) => item.id !== row.id));
      setPins((current) => insertByRecency(current, { ...row, pinned: true }));
    } else {
      setPins((current) => current.filter((item) => item.id !== row.id));
      setRecents((current) => insertByRecency(current, { ...row, pinned: false }));
    }
    try {
      await unwrap(
        api.PATCH("/v1/tasks/{task_id}", {
          params: { path: { task_id: row.id } },
          body: { pinned: next },
        }),
      );
    } catch (error) {
      if (next) {
        setPins((current) => current.filter((item) => item.id !== row.id));
        setRecents((current) => insertByRecency(current, row));
      } else {
        setRecents((current) => current.filter((item) => item.id !== row.id));
        setPins((current) => insertByRecency(current, row));
      }
      toastError(error, "Couldn't update the pin.");
    }
  }, []);

  const remove = useCallback(
    async (id: string): Promise<boolean> => {
      // Drop the row immediately; a delete is a decision, and a lingering row reads as failure.
      // A refusal (a live task answers 409) puts it back where it was: the task still exists.
      const row = [...pins, ...recents].find((item) => item.id === id);
      setPins((current) => current.filter((item) => item.id !== id));
      setRecents((current) => current.filter((item) => item.id !== id));
      try {
        await unwrap(api.DELETE("/v1/tasks/{task_id}", { params: { path: { task_id: id } } }));
        return true;
      } catch (error) {
        if (row) (row.pinned ? setPins : setRecents)((current) => insertByRecency(current, row));
        toastError(error, "Couldn't delete the task.");
        return false;
      }
    },
    [pins, recents],
  );

  const latest = useMemo(() => mergeTaskWindow(pins, recents), [pins, recents]);
  const value = useMemo(
    () => ({ pins, recents, latest, rename, togglePin, remove, patch: patchRow }),
    [latest, pins, recents, rename, togglePin, remove, patchRow],
  );

  return <TaskWindowContext.Provider value={value}>{children}</TaskWindowContext.Provider>;
}

export function useTaskWindow(): TaskWindowValue {
  const value = useContext(TaskWindowContext);
  if (value === null) {
    throw new Error("useTaskWindow must be used inside TaskWindowProvider");
  }
  return value;
}
