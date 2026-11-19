/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// A Project's name, remembered for the session. The task page's crumb ("Projects > Launch > the
// task") needs it, and the Project page and the sidebar already hold it - so the task loader asks
// here first and only goes to the API for a Project nobody has named yet, once. A rename is
// overwritten the next time the Project page or the shell loads (both feed this).

const names = new Map<string, string>();

export const knownProjectName = (id: string): string | undefined => names.get(id);

export function rememberProjectName(id: string, name: string): void {
  names.set(id, name);
}

export function forgetProjectName(id: string): void {
  names.delete(id);
}
