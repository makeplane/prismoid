# ADR-081 · Playwright smoke flows against a real stack, and a web coverage floor

**Status:** accepted · 2026-11-05 · the code-quality pass before the repository goes public.
**Amends** ADR-079 (its point 4: web coverage now has a floor).

## Context

The web suite tests components in happy-dom with the API mocked (ADR-079). Nothing drove the
built app in a browser against a running API and worker, so a broken route map, a CORS or session
regression, or a contract mismatch between the SPA and the API could pass every gate. ADR-079 left
"Playwright smoke flows" as the next step and web coverage without a floor.

## Decision

1. **`@playwright/test`, Chromium only, in `web/`.** `web/playwright.config.ts` and the flows in
   `web/e2e/*.spec.ts`. Five flows a person reaches first: sign in (and a wrong password, and sign
   out), create a project and open it from the list, edit the profile in settings and switch the
   appearance, the design-system page (`/ds`), and a task record. Elements are found by role and
   name, as in the DOM tests.
2. **No model, no secret.** `e2e/global-setup.ts` seeds through the API's own doors: a new account
   each run, and a task minted by firing a routine with posture `hold`. A held task is an ordinary
   record that waits for a direction, so no worker plans or runs it. A flow never submits the
   composer: an ordinary task would plan, start a Machine and call a model.
3. **The app is served as it ships.** Playwright's `webServer` runs `react-router build` with
   `VITE_PRISMOID_API_URL` pointed at the stack's API, then `vite preview` serves `build/client`. The
   dev server was rejected: its dependency optimizer reloads the page mid-test on a cold cache.
4. **`just e2e` brings its own stack.** `web/e2e/stack.sh` starts Postgres and MinIO containers,
   migrates and seeds, starts the worker and then the API on ports away from the dev stack's,
   runs the flows and removes everything it started, pass or fail. `E2E_STACK=external` runs the
   flows against an API that is already up. The recipe is not part of `just check`.
5. **CI: nightly and by hand.** `.github/workflows/e2e.yml` runs `just e2e` on Blacksmith at
   04:00 UTC and on `workflow_dispatch`, and keeps the report, traces and stack logs when it
   fails. It needs no secret, so a fork runs it too. Not on every PR: the stack boot and the
   web build cost minutes, and the DOM tests carry the per-PR load.
6. **A web coverage floor.** `web/vitest.config.ts` sets `coverage.thresholds` just under the
   measured numbers (about a point under, so a merge that moves a few lines does not flake), and
   `pnpm test:coverage` fails when coverage falls under them. Raise them when coverage rises;
   never lower them to make a change pass. `just check` runs the tests without coverage, so the
   floor holds where `test:coverage` runs; putting it on the CI gate is a separate change.

## Alternatives rejected

- **Cypress:** it runs inside the page with its own command chain, a second way of writing tests;
  Playwright's role locators read like the Testing Library queries the DOM tests already use.
- **Flows against `prismoid up`:** local mode is macOS-only (the device driver), and CI runs Linux.
- **A fake model behind the worker:** no such seam exists outside the Python tests, and adding one
  to the product for a smoke suite is the wrong trade. The held-routine seed proves the worker and
  the API without one.

## Consequences

- One dev dependency, `@playwright/test`, with a HANDOFF §3 row; Chromium's headless shell is
  downloaded by `playwright install`, not by `pnpm install`.
- A flow that needs a run (a delivered answer, a gate) waits for a model seam; until then the task
  record flow covers the record's rendering only.
