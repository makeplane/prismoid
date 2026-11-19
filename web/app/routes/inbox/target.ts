/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// Where an inbox item's Open action lands when it is not the item's task.

type ProjectKnock = {
  kind: string;
  project_id: string | null;
  payload: Record<string, unknown> | null;
};

type Knock = ProjectKnock & { task_id?: string | null };

/** The Project a notification is about, or null. A PROJECT_SHARED knock ("Ada added
 *  you to Launch") has no task and names its Project in its payload, so Open lands there; every
 *  other kind takes the Project its row carries (callers check `task_id` first, since a task's
 *  item carries its Project too but opens the task). */
export function projectTarget(item: ProjectKnock): string | null {
  if (item.kind === "project_shared") {
    const named = item.payload?.project_id;
    if (typeof named === "string" && named) return named;
  }
  return item.project_id ?? null;
}

/** What a gate knock says when no question is left to answer. A device wait holds no questions, and
 *  the task has not moved on: it is parked until its computer is back. */
export function noQuestionsLine(view: { deviceAway: { title: string } | null } | null): string {
  return view?.deviceAway
    ? `${view.deviceAway.title}. Resume the task once it is back.`
    : "These questions are settled; the task has moved on.";
}

/** A routine knock (paused / retired / proposed) has no task or project - its payload names the
 *  routine, and Open must land on /routines?routine= or a notification that says "resume it"
 *  dead-ends. */
export function routineTarget(item: ProjectKnock): string | null {
  const rid = item.payload?.routine_id;
  return typeof rid === "string" && rid ? `/routines?routine=${rid}` : null;
}

/** A connector knock has no task or project either - its payload names the slug, and Open lands
 *  on /connectors with the row's peek open. */
export function connectorTarget(item: ProjectKnock): string | null {
  if (item.kind !== "connector_broken") return null;
  const slug = item.payload?.connector_slug;
  return typeof slug === "string" && slug ? `/connectors?connector=${slug}` : "/connectors";
}

/** A waiting card's knock names its bot; the card sits in that bot's chat. */
export function botTarget(item: ProjectKnock): string | null {
  if (item.kind !== "card_waiting") return null;
  const name = item.payload?.bot_name;
  return typeof name === "string" && name ? `/bots/${encodeURIComponent(name)}` : null;
}

/** The selected notification's Open button label, or null when it offers none. */
export function openLabel(item: Knock): string | null {
  if (item.task_id) return "Open task";
  if (projectTarget(item)) return "Open project";
  if (botTarget(item)) return "Open bot";
  return routineTarget(item) ? "Open routine" : null;
}
