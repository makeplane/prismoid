/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

// Standalone on purpose: vitest would otherwise load vite.config.ts, and the react-router dev
// plugin there wants a full app context that focused unit/render tests neither have nor need.
// Tests run in node; a file that needs a DOM (interaction tests with Testing Library) opts in
// with a `// @vitest-environment happy-dom` line at its top, so the rest stay as fast as they are.
export default defineConfig({
  resolve: {
    alias: { "~": fileURLToPath(new URL("./app", import.meta.url)) },
  },
  test: {
    include: ["app/**/*.test.{ts,tsx}"],
    coverage: {
      provider: "v8",
      include: ["app/**/*.{ts,tsx}"],
      exclude: [
        "app/**/*.test.{ts,tsx}",
        "app/**/*.fixture.ts",
        "app/**/*.fixtures.ts",
        "app/lib/api/*.gen.ts",
      ],
      reporter: ["text-summary", "html"],
      // The floor: about a point under the last measured numbers, so coverage can only go up.
      // Raise these when it rises; never lower them to make a change pass (ADR-081).
      thresholds: { statements: 73, branches: 67, functions: 66, lines: 74 },
    },
  },
});
