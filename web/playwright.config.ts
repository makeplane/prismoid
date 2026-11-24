/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { defineConfig, devices } from "@playwright/test";

// The browser flows (ADR-081): a real browser against a real API and worker. The core projects
// (`app`, `admin`) run with no model behind them: `just e2e` boots a private stack (web/e2e/stack.sh)
// with every provider key blanked and runs them; against a stack you already run, set E2E_API_URL
// and call `pnpm e2e`. The `models` project (e2e/models/) exists only with E2E_MODELS=1: its flows
// send real work to a real model and spend a few cents each, so `just e2e-models` runs it alone, on
// demand, with the stack's worker holding a provider key. Both web apps are built for that API and served
// as the static SPAs they ship as, because the dev server's dependency optimizer reloads the page
// mid-test on a cold cache: the product on E2E_WEB_PORT, the operator console (its own bundle,
// APP_TARGET=admin) on E2E_ADMIN_WEB_PORT. E2E_REUSE_WEB=1 keeps servers already listening there.
const API_URL = process.env.E2E_API_URL ?? "http://127.0.0.1:8304";
const WEB_PORT = Number(process.env.E2E_WEB_PORT ?? 5304);
const ADMIN_WEB_PORT = Number(process.env.E2E_ADMIN_WEB_PORT ?? 5305);
const WEB_URL = `http://localhost:${WEB_PORT}`;
const ADMIN_WEB_URL = `http://localhost:${ADMIN_WEB_PORT}`;
const reuseExistingServer = process.env.E2E_REUSE_WEB === "1";
const withModels = process.env.E2E_MODELS === "1";

export default defineConfig({
  testDir: "./e2e",
  globalSetup: "./e2e/global-setup.ts",
  // One worker: the shared seed account's flows edit it in order, and one stack serves the run.
  // Every flow that changes an account signs up its own (fixtures.ts), so the order never leaks.
  workers: 1,
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  timeout: 45_000,
  expect: { timeout: 10_000 },
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : [["list"]],
  use: {
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "app",
      testIgnore: [/admin\//, /models\//],
      use: { ...devices["Desktop Chrome"], baseURL: WEB_URL },
    },
    {
      name: "admin",
      testMatch: /admin\/.*\.spec\.ts/,
      use: { ...devices["Desktop Chrome"], baseURL: ADMIN_WEB_URL },
    },
    // A model run settles in seconds, but a flow waits on several of them, and a provider has slow
    // minutes: each flow gets four minutes, each wait for a run its own long timeout.
    ...(withModels
      ? [
          {
            name: "models",
            testMatch: /models\/.*\.spec\.ts/,
            timeout: 240_000,
            use: { ...devices["Desktop Chrome"], baseURL: WEB_URL },
          },
        ]
      : []),
  ],
  webServer: [
    {
      command: `react-router build && vite preview --port ${WEB_PORT} --strictPort --outDir build/client`,
      url: WEB_URL,
      env: { VITE_PRISMOID_API_URL: API_URL },
      reuseExistingServer,
      timeout: 180_000,
    },
    {
      command: `react-router build && vite preview --port ${ADMIN_WEB_PORT} --strictPort --outDir build-admin/client`,
      url: ADMIN_WEB_URL,
      env: { VITE_PRISMOID_API_URL: API_URL, APP_TARGET: "admin" },
      reuseExistingServer,
      timeout: 180_000,
    },
  ],
});
