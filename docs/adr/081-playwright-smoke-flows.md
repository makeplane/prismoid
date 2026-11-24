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

## Amendment 2026-11-22 - every feature that needs no model, the console included

The five smoke flows grow into the browser suite for every surface a person reaches without a model
run: sign-in, signup, the password reset and the address proof, the shell, settings (memory, API
keys, the workspace and its members), workspaces and their invites, the home composer up to its
send, the task list, a held or cancelled task's page, projects with their people, invite links and
settings, routines, the inbox, bots up to publish (configure, memory, channels, the chat surface
without a message), connectors and allowed sites, and the operator console. Point 2 stands for
these core flows (the `app` and `admin` projects): none calls a model, so a core flow still never
sends the composer with When = Now, never sends in a held task's composer, and never fires a
routine that is not held. The model flows below are a separate project with their own run.

1. **A fresh account per flow.** A flow that changes an account (a reset revokes every session, a
   workspace switch rebinds one) or asserts an empty state signs up its own through the API
   (`e2e/support/accounts.ts`), so no flow depends on another's rows and a rerun on the same
   database never collides. The shared seed stays for the flows that only read. `stack.sh` widens
   the per-address signup, login and reset burst buckets on its private stack only; against a
   stack someone else runs, `call` waits out a 429.
2. **Emailed codes come from the API's log.** In dev the console mail backend prints each email to
   the API's stdout, which `stack.sh` writes to `E2E_API_LOG`; `e2e/support/mailbox.ts` reads the
   reset, verification and invite codes from there. Without that log the mail flows skip.
3. **The operator console is its own project.** Playwright serves the console bundle
   (`APP_TARGET=admin`) beside the product on `E2E_ADMIN_WEB_PORT`, and `stack.sh` adds its origin
   to CORS. The operator scope has no web door outside local mode, so global setup grants it the
   way an operator grants the first one, `prismoid admin bootstrap`, which needs the API's
   database URL; without it the console flows skip.
4. **A known bug is a flow expected to fail.** `knownBug(reason)` marks a flow that asserts the
   right behaviour and fails until the bug is fixed (`test.fail`), so a fix shows as an unexpected
   pass and the mark comes off in the same change. `E2E_SHOW_BUGS=1` runs them as ordinary flows.
5. **Two runs against one stack.** `E2E_REUSE_WEB=1` keeps web servers already listening, and
   `E2E_STATE_DIR` moves the seed file, so runs in parallel never share one.

## Amendment 2026-11-22 - the model flows, on a real model, by hand

The core flows above prove every surface up to a model's answer. What happens after it (a run's
thread, a document and its receipt, questions, a bot's reply) had no browser flow at all, so a
contract break between the stream, the worker and the task page passed every gate. Those flows
now exist, run against a real model, and stay out of every automatic run.

1. **A third project, `models`, defined only with `E2E_MODELS=1`.** Its flows live in
   `web/e2e/models/` and use `e2e/support/runs.ts`; the `app` project ignores the folder, and
   without the variable the project does not exist, so `just e2e`, `pnpm e2e` and the nightly job
   never see it. `just e2e-models` runs it alone on the same private stack. Each flow gets four
   minutes, and each wait for a run its own two.
2. **A real provider key, never a fake model.** The rejection of a fake model behind the worker
   stands: the model flows use the provider the product uses, through the catalog `stack.sh`
   seeds. `stack.sh` refuses to start the model run without a key in the environment
   (`PRISMOID_WORKER_ANTHROPIC_API_KEY`, or the OpenAI or Google one), and the core run now blanks
   all three (an exported empty value beats a key in `backend/.env`), so a core flow cannot reach a
   model even by accident.
3. **Flows that settle on paper.** Every flow is small enough to finish without a Machine: a
   question and its answer, a follow-up (the same task's next turn), a fork launched by a
   direction, a Markdown document opened in the reader with its verification receipt, a round of
   clarifying questions (Clarifying questions = Always opens one batch deterministically), a bot's
   chat reply, its selection evals turning green and the publish they unlock, the bot drafter, and
   a routine's Run now delivering to its owner. The document's verification runs in a throwaway
   frame (not a Machine) on the compose frame network, so `stack.sh` refuses the model run without
   that network (`just up` makes it, with its egress proxy), and the workflow starts the proxy and
   builds the base frame image first, as the nightly frame tests do. A Word, PDF or spreadsheet
   deliverable takes a Machine, slower and dearer (a Word file took 53 s and 16 model calls), so
   those stay out. `stack.sh` removes any frame container wearing its instance label when it ends.
4. **Assert what the server records, not the model's words.** A flow waits on a status, a message
   row or a version, and asserts words only where any sane answer must carry them (Paris for the
   capital of France). An unsourced note may come back unverified, so the receipt's presence is
   asserted, never its verdict. The question helper follows the panel rather than a fixed count,
   because the model writes the questions.
5. **By hand, from its own workflow.** `.github/workflows/e2e-models.yml` runs `just e2e-models` on
   `workflow_dispatch` only, with the repository secret `E2E_ANTHROPIC_API_KEY`, and stops at its
   first step without it. A run costs about twenty cents on the shipped catalog. Before uploading
   the report it drops any file that carries the key. A fork has no secrets, so it cannot run it.

Not covered yet: a run that takes a Machine (shell work, files in Word, PDF, spreadsheet, deck or
code, the screen, Live now), steering and pause (a run settles before a person can steer it), fleet
mode, approvals (they need a connector or a destructive tool), a bot's proposals, work cards and Save
as file, the template share and install that publish unlocks, connector authorization against a live
MCP server, and memory a delivered task writes (the extractor decides, so a flow cannot count on it).
