/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The deployment's public face (ADR-086): GET /v1/auth/config, read before anyone is signed in.
// Fetched once per page load and kept in module scope; a failed read is not cached and falls back
// to "open" with no links, because the server still enforces the real answer on submit.
import { api, unwrap } from "~/lib/api/client";
import type { components } from "~/lib/api/types.gen";

export type AuthConfig = components["schemas"]["AuthConfigOut"];

export const OPEN_CONFIG: AuthConfig = {
  signup: "open",
  terms_url: null,
  privacy_url: null,
  support_email: null,
};

let cached: Promise<AuthConfig> | null = null;

export function getAuthConfig(): Promise<AuthConfig> {
  cached ??= unwrap(api.GET("/v1/auth/config")).catch(() => {
    cached = null;
    return OPEN_CONFIG;
  });
  return cached;
}

/** The form is hidden only when signup is invite-only and the visitor holds no invite link. */
export function signupPaused(config: AuthConfig, invite: string): boolean {
  return config.signup === "invite" && !invite;
}
