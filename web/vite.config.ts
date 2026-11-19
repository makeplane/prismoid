/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { reactRouter } from "@react-router/dev/vite";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";

export default defineConfig({
  // Per-target dep cache, mirroring react-router.config.ts's buildDirectory split. The two dev
  // servers (product :5173, admin :5174) run concurrently from this one root, and Vite's dep
  // optimizer rewrites its cache in place: sharing the default node_modules/.vite let either
  // server invalidate chunk hashes the other was serving - the client's dynamic route imports
  // then 404 and react-router surfaces "No result returned from dataStrategy" in a reload loop
  // (seen live 2026-08-25, the first day both apps ran side by side).
  cacheDir: process.env.APP_TARGET === "admin" ? "node_modules/.vite-admin" : "node_modules/.vite",
  plugins: [tailwindcss(), reactRouter()],
  resolve: {
    tsconfigPaths: true,
  },
});
