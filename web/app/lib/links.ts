/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// Where things live in the address bar - ONE place that spells a task's URL.
//
// A Project task is canonical at /projects/<sid>/tasks/<tid>; a personal task (no Project) at
// /tasks/<tid>. /tasks/<tid> keeps working for a Project task - inbox
// items, chat cards, CLI output, emails and bookmarks all spell it - the task page's loader
// redirects it (canonicalTaskPath). The project segment is CONTEXT, never authority: the server
// authorizes by task id, so a wrong or garbage segment only ever redirects.
//
// Every in-app link to a task goes through taskHref. Where a row does not carry project_id (a
// fork's parent, a memory's source, a bot card), pass what is there: the redirect lands the
// reader on the canonical URL anyway.

/** What a task link needs: its id, and its Project when the row knows it. */
export type TaskRef = { id: string; project_id?: string | null };

export const projectHref = (id: string): string => `/projects/${encodeURIComponent(id)}`;

/** Where a new task starts: Home's composer, which takes focus on arrival. */
export const NEW_TASK_HREF = "/";

/** The canonical URL of a task. */
export function taskHref(task: TaskRef): string {
  const tail = `/tasks/${encodeURIComponent(task.id)}`;
  return task.project_id ? `${projectHref(task.project_id)}${tail}` : tail;
}

/** Is `pathname` the base itself or somewhere below it? ("/projects/s1" is within "/projects/s1",
 *  "/projects/s1/tasks/t", and never "/projects/s10".) */
export function isWithin(pathname: string, base: string): boolean {
  return pathname === base || pathname.startsWith(`${base}/`);
}

const trimSlash = (path: string): string =>
  path.length > 1 && path.endsWith("/") ? path.slice(0, -1) : path;

/**
 * The redirect decision for a task page: `null` when `currentPath` already IS the task's
 * canonical URL, otherwise where to go instead. `currentPath` may carry `?search` and `#hash`;
 * neither decides, and both ride along on a redirect.
 */
export function canonicalTaskPath(currentPath: string, task: TaskRef): string | null {
  const cut = currentPath.search(/[?#]/);
  const pathname = cut === -1 ? currentPath : currentPath.slice(0, cut);
  const suffix = cut === -1 ? "" : currentPath.slice(cut);
  const target = taskHref(task);
  return trimSlash(pathname) === target ? null : `${target}${suffix}`;
}

/** Where a task page's loader sends the reader, and HOW. */
export type TaskRedirect = { to: string; replace: boolean };

/**
 * The task page loader's whole redirect decision. `request` is the URL being loaded; `entry` is
 * the address bar right now (null when there is no window to ask).
 *
 * REPLACE only when the bar already shows this very URL - a fresh load, Back/Forward onto it, a
 * revalidation - because then the entry IS the wrong address and replacing it leaves no bounce.
 * Anywhere else the reader clicked here from another page, and replacing would overwrite that
 * page in history (Back would skip it): PUSH, so history reads [origin, canonical].
 *
 * The router strips #fragment from a loader's request, so the bar's is the only copy - and it is
 * this navigation's own only when the bar shows this URL. Otherwise it belongs to the origin and
 * is dropped.
 */
export function taskRedirect(
  request: { pathname: string; search: string },
  entry: { pathname: string; hash: string } | null,
  task: TaskRef,
): TaskRedirect | null {
  const here = entry !== null && entry.pathname === request.pathname;
  const to = canonicalTaskPath(
    `${request.pathname}${request.search}${here ? entry.hash : ""}`,
    task,
  );
  return to === null ? null : { to, replace: here };
}
