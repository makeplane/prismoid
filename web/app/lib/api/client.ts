/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The one typed client. openapi-fetch over the generated `paths`, so every request/response is checked
// against docs/api/openapi.json - the frozen contract. Middleware injects the bearer, checks the
// X-Prismoid-Protocol header (a mismatch means the client is talking to a different contract version),
// and a 401 anywhere triggers the app-wide sign-out. `unwrap` turns the {data,error} tuple into a value
// or a typed ApiError carrying the error envelope (code, message, request_id, fields, retry_after).
import { toast } from "@prismoid/blocks/toast";
import createClient, { type Middleware } from "openapi-fetch";
import type { paths } from "./types.gen";
import { STORAGE } from "~/lib/storage";

/** The contract version this client speaks; the API answers it in X-Prismoid-Protocol. */
export const API_PROTOCOL = "3";

export const API_URL: string =
  (import.meta.env.VITE_PRISMOID_API_URL as string | undefined) ?? "http://localhost:8100";

const TOKEN_KEY = STORAGE.token;
const EXPIRES_KEY = STORAGE.expires;

export function getToken(): string | null {
  return typeof localStorage === "undefined" ? null : localStorage.getItem(TOKEN_KEY);
}
export function getExpiresAt(): string | null {
  return typeof localStorage === "undefined" ? null : localStorage.getItem(EXPIRES_KEY);
}
export function storeSession(token: string, expiresAt: string): void {
  localStorage.setItem(TOKEN_KEY, token);
  localStorage.setItem(EXPIRES_KEY, expiresAt);
}
export function clearSession(): void {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(EXPIRES_KEY);
}

/** What a stale steer's author had not seen (the 409 `stale_steer`), oldest first: a
 *  later steer (its author and a preview of its words) or the start of the next turn. */
export type MissedSteer = {
  kind: "steer" | "turn";
  author: string;
  text?: string;
  epoch: number;
  state?: string | null;
};

/** The error envelope, as a throwable. */
export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public requestId?: string,
    public fields?: Record<string, string>,
    public retryAfter?: number,
    public missed?: MissedSteer[],
  ) {
    super(message);
    this.name = "ApiError";
  }
}

let onUnauthorized: (() => void) | null = null;
/** Wired once at the root: any 401 clears the session and bounces to /login. */
export function setUnauthorizedHandler(fn: () => void): void {
  onUnauthorized = fn;
}

let onRateLimited: ((retryAfter?: number) => void) | null = null;
/** Wired once at the root: any 429 surfaces a toast with the live Retry-After countdown. */
export function setRateLimitedHandler(fn: (retryAfter?: number) => void): void {
  onRateLimited = fn;
}

const middleware: Middleware = {
  onRequest({ request }) {
    const t = getToken();
    if (t) request.headers.set("Authorization", `Bearer ${t}`);
    return request;
  },
  onResponse({ response }) {
    const proto = response.headers.get("X-Prismoid-Protocol");
    if (proto && proto !== API_PROTOCOL) {
      console.warn(
        `[prismoid] API protocol "${proto}" != client "${API_PROTOCOL}" - the frontend may be stale.`,
      );
    }
    return response;
  },
};

export const api = createClient<paths>({ baseUrl: API_URL });
api.use(middleware);

type Result<T> = { data?: T; error?: unknown; response: Response };

/** Unwrap an openapi-fetch result to its data, or throw an ApiError built from the envelope.
 *
 *  `localAuthError`: for the routes where a 401 is the form's own answer - a bad reset code on
 *  /reset-password - not a dead session. Without it the app-wide handler fires first, clears any
 *  stored session and hard-redirects to /login mid-submit, so the route's friendly 401 branch
 *  can never render. Opting out is per-request, at the mechanism, never a pathname special case. */
export async function unwrap<T>(
  p: Promise<Result<T>>,
  opts?: { localAuthError?: boolean },
): Promise<T> {
  const { data, error, response } = await p;
  // A 204 is the API's own "done, nothing to say" (openapi-fetch reports it as data: undefined),
  // so it is success - not an "error" that reads "No Content" to the person who just succeeded.
  if (response.ok && (data !== undefined || response.status === 204)) return data as T;
  const env = ((error as { error?: Record<string, unknown> } | undefined)?.error ?? {}) as Record<
    string,
    unknown
  >;
  const retryAfter = env.retry_after as number | undefined;
  if (response.status === 401 && !opts?.localAuthError) onUnauthorized?.();
  if (response.status === 429) onRateLimited?.(retryAfter);
  throw new ApiError(
    response.status,
    (env.code as string) ?? "error",
    (env.message as string) ?? response.statusText,
    env.request_id as string | undefined,
    plainFieldErrors(env.fields as Record<string, string> | undefined),
    retryAfter,
    env.missed as MissedSteer[] | undefined,
  );
}

/** A field error as a person reads it. FastAPI's 422 carries pydantic's own frame ("value is
 *  not a valid email address: The part after the @-sign is a special-use or reserved name…",
 *  "Value error, …"); the reason after it is the part worth showing under a field. */
export function plainFieldErrors(
  fields: Record<string, string> | undefined,
): Record<string, string> | undefined {
  if (!fields) return fields;
  const plain = (msg: string) =>
    msg.replace(/^value is not a valid [a-z ]+: /i, "").replace(/^Value error, /, "");
  return Object.fromEntries(Object.entries(fields).map(([k, v]) => [k, plain(String(v))]));
}

/** Health for the dev footer. */
export async function ping(): Promise<boolean> {
  try {
    const r = await fetch(`${API_URL}/healthz`);
    return r.ok;
  } catch {
    return false;
  }
}

/** The one error-toast incantation (once retyped everywhere). API errors speak
 *  for themselves; anything else gets the caller's plain-words fallback. */
export function toastError(e: unknown, fallback: string): void {
  toast.error(e instanceof ApiError ? e.message : fallback);
}
