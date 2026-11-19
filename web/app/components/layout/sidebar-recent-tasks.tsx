/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { Button } from "@makeplane/propel/components/button";
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
import { Icon } from "@makeplane/propel/components/icon";
import {
  Menu,
  MenuCheckboxItem,
  MenuContent,
  MenuItem,
  MenuTrigger,
} from "@makeplane/propel/components/menu";
import { ArrowRight, Pencil, Pin, PinOff, SlidersHorizontal, Trash2 } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link, useLocation, useNavigate, useRouteLoaderData } from "react-router";
import { useNavPath } from "~/hooks/use-pending-path";
import { StatusMark, taskMark } from "~/components/kit";
import { dayKey, dayLabel } from "~/lib/format";
import { taskHref } from "~/lib/links";
import { STORAGE } from "~/lib/storage";
import {
  rowVerbs,
  TASK_RECENT_WINDOW,
  useTaskWindow,
  type TaskWindowRow,
} from "~/components/tasks/task-window";
import {
  SidebarGroup,
  SidebarGroupAction,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarMenu,
  SidebarMenuAction,
  SidebarMenuButton,
  SidebarMenuItem,
} from "@prismoid/blocks/sidebar";

/** The sidebar shows a window, not the history: the endpoint keyset-paginates and the full
 *  list (with its own load-more) lives on /tasks - a rail that tried to hold everything
 *  would become the page it links to. When the window is full, a quiet "View all" tail says
 *  where the rest lives. */
/**
 * The conversation list: pinned tasks on top, the latest below, each with the machine's
 * status as a dot (pulsing while running - the status is the record's, never editable
 * here). Rows rename in place (⋯ → Rename) and pin/unpin from the same menu. Refetches on
 * navigation, so a task created a moment ago is already listed.
 */
export function SidebarRecentTasks() {
  const pathname = useNavPath();
  const { pins, recents, rename, togglePin } = useTaskWindow();
  // A teammate's Project task lists here too; its row verbs are the creator's (rowVerbs).
  const shell = useRouteLoaderData("routes/shell/route") as { user?: { id: string } } | undefined;
  const myId = shell?.user?.id;
  const [editing, setEditing] = useState<string | null>(null);
  const [confirming, setConfirming] = useState<TaskWindowRow | null>(null);

  // The section's display prefs (the options control beside the label): date grouping
  // and status marks. Persisted, purely presentational.
  const [groupByDate, setGroupByDate] = useState(
    () =>
      typeof window !== "undefined" && localStorage.getItem(STORAGE.sidebarTasksGroup) === "date",
  );
  const [showMarks, setShowMarks] = useState(
    () =>
      typeof window === "undefined" || localStorage.getItem(STORAGE.sidebarTasksMarks) !== "off",
  );
  const pickGroupByDate = (on: boolean) => {
    setGroupByDate(on);
    localStorage.setItem(STORAGE.sidebarTasksGroup, on ? "date" : "none");
  };
  const pickMarks = (on: boolean) => {
    setShowMarks(on);
    localStorage.setItem(STORAGE.sidebarTasksMarks, on ? "on" : "off");
  };

  if (pins.length === 0 && recents.length === 0) return null;

  // Day buckets over the (already newest-first) window - same vocabulary as the /tasks
  // Date grouping: Today, Yesterday, then dates.
  const dayGroups = groupByDate
    ? recents.reduce<{ key: string; label: string; rows: TaskWindowRow[] }[]>((acc, t) => {
        const d = new Date(t.createdAt);
        const key = dayKey(d);
        const last = acc[acc.length - 1];
        if (last?.key === key) last.rows.push(t);
        else acc.push({ key, label: dayLabel(d), rows: [t] });
        return acc;
      }, [])
    : null;

  const renderRow = (t: TaskWindowRow) => {
    const verbs = rowVerbs(t, myId);
    return (
      <SidebarMenuItem key={t.id}>
        {editing === t.id ? (
          <RenameField
            initial={t.label}
            onDone={(value) => {
              setEditing(null);
              void rename(t.id, value);
            }}
            onCancel={() => setEditing(null)}
          />
        ) : (
          <>
            <SidebarMenuButton
              isActive={pathname === taskHref({ id: t.id, project_id: t.projectId })}
              title={`${t.label} · ${t.status}`}
              render={<Link to={taskHref({ id: t.id, project_id: t.projectId })} />}
            >
              {showMarks && (
                <StatusMark status={taskMark(t.status, t.ending)} className="shrink-0" />
              )}
              <span className="truncate">{t.label}</span>
            </SidebarMenuButton>
            {(verbs.rename || verbs.pin || verbs.delete) && (
              <Menu>
                <MenuTrigger
                  render={
                    <SidebarMenuAction showOnHover aria-label={`Task actions for ${t.label}`} />
                  }
                >
                  ⋯
                </MenuTrigger>
                <MenuContent align="start">
                  {verbs.rename && (
                    <MenuItem
                      icon={<Icon icon={Pencil} />}
                      label="Rename"
                      onClick={() => setEditing(t.id)}
                    />
                  )}
                  {verbs.pin && (
                    <MenuItem
                      icon={<Icon icon={t.pinned ? PinOff : Pin} />}
                      label={t.pinned ? "Unpin" : "Pin"}
                      onClick={() => void togglePin(t)}
                    />
                  )}
                  {verbs.delete && (
                    <MenuItem
                      variant="danger"
                      icon={<Icon icon={Trash2} />}
                      label="Delete"
                      onClick={() => setConfirming(t)}
                    />
                  )}
                </MenuContent>
              </Menu>
            )}
          </>
        )}
      </SidebarMenuItem>
    );
  };

  const group = (title: string, children: ReactNode, action?: ReactNode) => (
    // group/section: the options control hides until the section is hovered or focused
    <SidebarGroup className="group/section group-data-[collapsible=icon]:hidden">
      <SidebarGroupLabel>{title}</SidebarGroupLabel>
      {action}
      <SidebarGroupContent>
        <SidebarMenu>{children}</SidebarMenu>
      </SidebarGroupContent>
    </SidebarGroup>
  );

  return (
    <>
      {confirming && <DeleteTaskDialog row={confirming} onClose={() => setConfirming(null)} />}
      {pins.length > 0 && group("Pinned", pins.map(renderRow))}
      {recents.length > 0 &&
        group(
          "Recent tasks",
          dayGroups
            ? dayGroups.flatMap((g) => [
                // a whisper of a day header between runs - same vocabulary as /tasks'
                // Date grouping (Today · Yesterday · 30 Jul)
                <li
                  key={`day-${g.key}`}
                  className="text-muted-foreground/70 px-2.5 pt-2 pb-0.5 text-caption-md-regular"
                >
                  {g.label}
                </li>,
                ...g.rows.map(renderRow),
              ])
            : recents.map(renderRow),
          // the section's ONE quiet options control: display
          // prefs only - grouping and marks; data verbs stay on the rows' own ⋯
          <Menu>
            <MenuTrigger
              render={
                <SidebarGroupAction
                  aria-label="Tasks section options"
                  // hidden until the section is hovered/focused (or the menu is open) -
                  // the SidebarMenuAction showOnHover recipe; always visible on touch
                  className="text-muted-foreground hover:text-foreground group-hover/section:opacity-100 group-focus-within/section:opacity-100 transition-opacity aria-expanded:opacity-100 md:opacity-0"
                />
              }
            >
              <SlidersHorizontal className="size-3.5" />
            </MenuTrigger>
            <MenuContent align="start">
              <MenuCheckboxItem
                checked={groupByDate}
                onCheckedChange={(c) => pickGroupByDate(c === true)}
                label="Group by date"
              />
              <MenuCheckboxItem
                checked={showMarks}
                onCheckedChange={(c) => pickMarks(c === true)}
                label="Show status"
              />
            </MenuContent>
          </Menu>,
        )}
      {recents.length >= TASK_RECENT_WINDOW && (
        <SidebarGroup className="-mt-2 group-data-[collapsible=icon]:hidden">
          <SidebarGroupContent>
            <SidebarMenu>
              <SidebarMenuItem>
                <SidebarMenuButton className="text-muted-foreground" render={<Link to="/tasks" />}>
                  <ArrowRight className="size-3.5" />
                  <span>View all tasks</span>
                </SidebarMenuButton>
              </SidebarMenuItem>
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
      )}
    </>
  );
}

/** A delete needs a confirm, and the sidebar cannot know if a task is terminal (the window row
 *  carries the status). A live task's delete is refused by the API; the toast surfaces the
 *  reason ("cancel the task before deleting it") rather than the sidebar guessing. If the deleted
 *  task is the one open, leave its now-dead page. */
function DeleteTaskDialog({ row, onClose }: { row: TaskWindowRow; onClose: () => void }) {
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const { remove } = useTaskWindow();
  const [busy, setBusy] = useState(false);

  async function confirm() {
    setBusy(true);
    const ok = await remove(row.id);
    setBusy(false);
    onClose();
    if (ok && pathname === taskHref({ id: row.id, project_id: row.projectId })) navigate("/tasks");
  }

  return (
    <AlertDialog open onOpenChange={(o) => !o && onClose()}>
      <AlertDialogContent>
        <AlertDialogBody>
          <AlertDialogHeader>
            <AlertDialogIntro>
              <AlertDialogTitle>Delete this task?</AlertDialogTitle>
              <AlertDialogDescription>
                “{row.label.slice(0, 80)}” and everything it produced are permanently removed. Your
                usage history is kept. This cannot be undone.
              </AlertDialogDescription>
            </AlertDialogIntro>
          </AlertDialogHeader>
        </AlertDialogBody>
        <AlertDialogActions>
          <Button
            variant="ghost"
            size="md"
            stretch="auto"
            onClick={onClose}
            disabled={busy}
            label="Cancel"
          />
          <Button
            variant="danger"
            size="md"
            stretch="auto"
            onClick={() => void confirm()}
            disabled={busy}
            icon={<Icon icon={Trash2} />}
            label="Delete task"
          />
        </AlertDialogActions>
      </AlertDialogContent>
    </AlertDialog>
  );
}

function RenameField({
  initial,
  onDone,
  onCancel,
}: {
  initial: string;
  onDone: (value: string) => void;
  onCancel: () => void;
}) {
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => {
    ref.current?.focus();
    ref.current?.select();
  }, []);
  return (
    <input
      ref={ref}
      defaultValue={initial}
      maxLength={60} // the title's hard lip - same as the wire's
      aria-label="Task name"
      className="border-ring ring-ring/30 flex h-8 w-full min-w-0 rounded-md border bg-transparent px-2 text-body-sm-regular ring-2 outline-none"
      onKeyDown={(e) => {
        if (e.key === "Enter") onDone(e.currentTarget.value);
        if (e.key === "Escape") onCancel();
      }}
      onBlur={(e) => onDone(e.currentTarget.value)}
    />
  );
}
