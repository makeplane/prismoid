/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The URL an event stream opens with. An EventSource cannot set an Authorization header, so the
// browser first mints a single-use, thirty-second ticket with an ordinary authenticated POST and
// puts that in the URL; the session token never rides it. A ticket opens one stream once, so
// every open and every reopen mints its own.

import { API_URL, ApiError, api, unwrap } from "./api/client";

export async function taskStreamUrl(taskId: string, since: number, conn: string): Promise<string> {
  const { ticket } = await unwrap(
    api.POST("/v1/tasks/{task_id}/events/ticket", { params: { path: { task_id: taskId } } }),
  );
  const query = `since=${since}&ticket=${encodeURIComponent(ticket)}&conn=${conn}`;
  return `${API_URL}/v1/tasks/${taskId}/events?${query}`;
}

export async function conversationStreamUrl(
  conversationId: string,
  since: number,
): Promise<string> {
  const { ticket } = await unwrap(
    api.POST("/v1/conversations/{conversation_id}/events/ticket", {
      params: { path: { conversation_id: conversationId } },
    }),
  );
  const query = `since=${since}&ticket=${encodeURIComponent(ticket)}`;
  return `${API_URL}/v1/conversations/${conversationId}/events?${query}`;
}

/** How long a stream that dropped waits before it mints a ticket and reopens: one second, doubling
 *  per failure to a ceiling, and back to the first wait once a stream opens. */
export const REOPEN_FIRST_MS = 1000;
export const REOPEN_MAX_MS = 30_000;

export function nextReopenWait(wait: number): number {
  return Math.min(wait * 2, REOPEN_MAX_MS);
}

/** Is a ticket mint's refusal final? A 404 (the subject's one fetch path: removed from its Project,
 *  deleted, never yours) or a 403 will answer the same on every retry, so the stream ends instead of
 *  backing off forever. A 401 has already signed the tab out; a 429, a 5xx or a network blip retries. */
export function isFinalRefusal(err: unknown): boolean {
  return err instanceof ApiError && (err.status === 404 || err.status === 403);
}
