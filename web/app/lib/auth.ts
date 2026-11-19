/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { redirect } from "react-router";
import type { ZodError } from "zod";
import { api, ApiError, clearSession, getExpiresAt, getToken, unwrap } from "./api/client";
import type { components } from "./api/types.gen";

/** The session's own profile - `/v1/auth/me` returns MeOut (the caller's profile PLUS their
 *  own admin standing), so the shell can gate the Admin nav entry instead of 403-probing. */
export type User = components["schemas"]["MeOut"];

const LOGIN = "/login";
const SIGNUP = "/signup";

// Any origin will do: a relative reference resolved against it stays on it exactly when it is a
// path of ours, and hops to another origin when it is a URL of somebody else's.
const BASE = "https://app.invalid";

/** A C0 control character (U+0000..U+001F) or DEL anywhere. No path of ours carries one: a
 *  browser silently drops some of them from a URL, and a NUL makes the redirect's Location header
 *  throw. Judged by code point, so the lint's no-control-regex rule has nothing to say. */
function hasControl(s: string): boolean {
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    if (c < 0x20 || c === 0x7f) return true;
  }
  return false;
}

/** One reading of "a path on this site": no control character, "/" then NOT another "/", a
 *  backslash or whitespace, and a WHATWG parse that lands on the same origin with a pathname that
 *  does not itself begin "//". Browsers drop tab and newline anywhere in a URL and read "\" as
 *  "/", so "/\evil.test/x", "/<TAB>/evil.test" and "/<LF>/evil.test" all resolve to
 *  https://evil.test/x. */
function isPathOfOurs(next: string): boolean {
  if (hasControl(next) || !/^\/(?![/\\\s])/.test(next)) return false;
  try {
    const url = new URL(next, BASE);
    return url.origin === BASE && !url.pathname.startsWith("//");
  } catch {
    return false;
  }
}

/**
 * THE `?next=` guard, one for every door that reads or writes it (login, signup, admin-login, the
 * join link's trip through sign-in): true only for a same-origin relative path. The value is judged
 * as it arrives AND once decoded ("/%09/evil.test" is "/<TAB>/evil.test" one decode later), so an
 * encoding cannot smuggle past a check that only reads the raw text. React Router falls back to
 * `location.assign` for an off-origin URL, so a hole here is an open redirect with no click.
 */
export function isSafePath(next: string | null | undefined): next is string {
  if (!next || !isPathOfOurs(next)) return false;
  let decoded = next;
  try {
    decoded = decodeURIComponent(next);
  } catch {
    // a stray "%" is not an encoding to worry about; the raw text already passed
  }
  return isPathOfOurs(decoded);
}

/**
 * The path a safe `next` names, as the URL parser writes it back: pathname + search + hash,
 * percent-encoded. A redirect's Location header takes ASCII only, and `?next=%2F%E6%97%A5` arrives
 * decoded as "/日" - handed back raw it would throw on the way out. Null when it is not ours.
 */
export function safePath(next: string | null | undefined): string | null {
  if (!isSafePath(next)) return null;
  const url = new URL(next, BASE);
  return url.pathname + url.search + url.hash;
}

/**
 * Build `/login?next=<path>` so a deep link survives the round-trip through sign-in. We never carry
 * `/`, `/login`, or `/signup` as a next target (nothing to return to), and only a same-origin path
 * is ever preserved (`isSafePath`).
 */
export function loginHref(nextPath?: string): string {
  const path = safePath(nextPath);
  if (path == null || path === "/" || path.startsWith(LOGIN) || path.startsWith(SIGNUP)) {
    return LOGIN;
  }
  return `${LOGIN}?next=${encodeURIComponent(path)}`;
}

/** `/signup`, carrying the same `next` the login page holds, so a person who has no account yet
 *  and creates one lands back where the link was taking them. */
export function signupHref(nextPath?: string): string {
  const path = safePath(nextPath);
  if (path == null || path === "/") return SIGNUP;
  return `${SIGNUP}?next=${encodeURIComponent(path)}`;
}

/** Where sign-in goes after it succeeds: the requested path when it is ours, otherwise home. */
export function safeNext(next: string | null | undefined): string {
  return safePath(next) ?? "/";
}

/**
 * The routing-layer guard. For a protected loader: ensure a live session or throw a redirect to
 * `/login?next=`. We don't trust the mere presence of a token - it's validated against GET
 * /v1/auth/me, so a stale/revoked token can never paint protected UI. Network errors propagate to the
 * route's error boundary rather than masquerading as "logged out".
 */
export async function requireUser(request: Request): Promise<User> {
  const url = new URL(request.url);
  const here = url.pathname + url.search;
  if (!getToken()) {
    throw redirect(loginHref(here));
  }
  try {
    return await unwrap(api.GET("/v1/auth/me"));
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) {
      clearSession();
      throw redirect(loginHref(here));
    }
    throw e;
  }
}

/** Milliseconds until the stored token expires, or null when the expiry is unknown/unparseable. */
export function msUntilExpiry(): number | null {
  const exp = getExpiresAt();
  if (!exp) return null;
  const t = Date.parse(exp);
  return Number.isNaN(t) ? null : t - Date.now();
}

/** First message per field from a zod error - the {field: message} shape our forms render. Reads
 *  `.issues` directly so it's stable across zod's flatten-API churn. */
export function zodFieldErrors(error: ZodError): Record<string, string> {
  const out: Record<string, string> = {};
  for (const issue of error.issues) {
    const key = String(issue.path[0] ?? "");
    if (key && !(key in out)) out[key] = issue.message;
  }
  return out;
}
