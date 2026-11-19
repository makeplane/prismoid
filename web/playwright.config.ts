/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { defineConfig, devices } from "@playwright/test";

// The smoke flows (ADR-081): a real browser against a real API and worker, with no model behind
// them. `just e2e` boots a private stack (web/e2e/stack.sh) and runs this; against a stack you
// already run, set E2E_API_URL and call `pnpm e2e`. The web app is built for that API and served
// as the static SPA it ships as, because the dev server's dependency optimizer reloads the page
// mid-test on a cold cache.
const API_URL = process.env.E2E_API_URL ?? "http://127.0.0.1:8304";
const WEB_PORT = Number(process.env.E2E_WEB_PORT ?? 5304);
const WEB_URL = `http://localhost:${WEB_PORT}`;

export default defineConfig({
  testDir: "./e2e",
  globalSetup: "./e2e/global-setup.ts",
  // One worker: the flows share one account and the API's per-person auth buckets.
  workers: 1,
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  timeout: 30_000,
  expect: { timeout: 10_000 },
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : [["list"]],
  use: {
    baseURL: WEB_URL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: `react-router build && vite preview --port ${WEB_PORT} --strictPort --outDir build/client`,
    url: WEB_URL,
    env: { VITE_PRISMOID_API_URL: API_URL },
    reuseExistingServer: false,
    timeout: 180_000,
  },
});
