/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The settings dialog is owned by the sidebar (its account menu deep-links into sections),
// but other surfaces legitimately need to talk to it - the composer's Connectors control
// lands on Settings → Connectors, and the panel announces roster changes back.
// One module-level EventTarget keeps those links without threading dialog state through
// every route: dispatchers stay ignorant of who renders the dialog, the owners subscribe,
// and (unlike window) the bus needs no DOM, so it unit-tests in plain node.

import type { SettingsSection } from "~/components/layout/settings-dialog";

const bus = new EventTarget();

const OPEN_EVENT = "plane:open-settings";
const CLOSE_EVENT = "plane:close-settings";
const CONNECTORS_EVENT = "plane:connectors-changed";

export function openSettings(section: SettingsSection = "general"): void {
  bus.dispatchEvent(new CustomEvent<SettingsSection>(OPEN_EVENT, { detail: section }));
}

export function onOpenSettings(listen: (section: SettingsSection) => void): () => void {
  const handler = (e: Event) => listen((e as CustomEvent<SettingsSection>).detail ?? "general");
  bus.addEventListener(OPEN_EVENT, handler);
  return () => bus.removeEventListener(OPEN_EVENT, handler);
}

/** The reverse door: a panel that hands off to a full ROUTE (Connectors → the
 *  /connectors page) must be able to put the dialog away first - navigating under an open
 *  modal leaves the page inert behind it. */
export function closeSettings(): void {
  bus.dispatchEvent(new Event(CLOSE_EVENT));
}

export function onCloseSettings(listen: () => void): () => void {
  bus.addEventListener(CLOSE_EVENT, listen);
  return () => bus.removeEventListener(CLOSE_EVENT, listen);
}

/** The return path: the Connectors panel announces every roster mutation (add, remove, a
 *  grant landing, a probe going live) so surfaces holding a FETCHED catalog - the task
 *  page's fresh-turn dial - refetch instead of dead-ending on a stale roster. Loader-fed
 *  surfaces refresh via useRevalidator; this event is for component state. */
export function emitConnectorsChanged(): void {
  bus.dispatchEvent(new Event(CONNECTORS_EVENT));
}

export function onConnectorsChanged(listen: () => void): () => void {
  bus.addEventListener(CONNECTORS_EVENT, listen);
  return () => bus.removeEventListener(CONNECTORS_EVENT, listen);
}
