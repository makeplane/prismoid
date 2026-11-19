/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import type { Config } from "@react-router/dev/config";

export default {
  // SPA mode (§plan): no runtime server. The root route is prerendered into index.html at build
  // time; every route loads data client-side (clientLoader/clientAction) against the API.
  ssr: false,
  // Two applications, one codebase (see app/routes.ts): the operator console builds to its own
  // directory so the two bundles can be shipped as separate containers on separate domains.
  buildDirectory: process.env.APP_TARGET === "admin" ? "build-admin" : "build",
} satisfies Config;
