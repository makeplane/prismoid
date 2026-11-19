# ADR-079 · A DOM for interaction tests: happy-dom, Testing Library, user-event, V8 coverage

**Status:** accepted · 2026-11-04 · the code-quality pass before the repository goes public.
**Amends** ADR-073 (the formatter's `printWidth` is now 100, and the linter runs the jsx-a11y
rules).

## Context

The web suite ran every test in node. Components were tested by rendering them to static markup
(`renderToStaticMarkup`) and matching strings: that proves what a surface shows, never what it
does. Nothing pressed a button, typed into the composer, moved focus with Tab or resolved a gate,
so a keyboard path could break without a test failing. `packages/blocks` had two tests for its
ten component modules. There was no coverage tool for the JS half at all.

## Decision

1. **happy-dom, per file.** A test that needs a DOM starts with
   `// @vitest-environment happy-dom` and is named `*.dom.test.tsx` in `web/`. Every other test
   keeps running in node, so the existing ~1,200 tests stay as fast as they are and nothing about
   them changes. jsdom was the alternative; happy-dom starts faster and covers what Base UI and
   Testing Library use.
2. **Testing Library and user-event.** A DOM test renders with `@testing-library/react`, finds
   elements by role and accessible name, and drives them with `@testing-library/user-event`
   (real key sequences: Tab, Enter, Space, the arrows). Querying by role makes each interaction
   test an accessibility check as well: an icon button with no name cannot be found, which is how
   the composer's unnamed Stop and Dictate buttons surfaced (a text tooltip now names an
   icon-only `PromptInputButton`). Each file calls
   `afterEach(cleanup)` itself (the suite runs without vitest globals).
3. **The first interaction tests** cover the surfaces with the most rules: the composer (Enter,
   Shift+Enter, a failed send keeps the draft, one Enter sends once), the composer as the steering
   bar of a live task and the stale-steer strip, the gate card's resolve (answers, decline, cancel,
   an approval's scope by arrow keys, a failed resolve), inbox triage (arrow keys, Enter, mark
   read), and the collection lenses' keyboard-pressable records. `packages/blocks` gets a test
   per exported component module (props, roles, slots).
4. **V8 coverage.** `pnpm test:coverage` (web and blocks) runs vitest with `@vitest/coverage-v8`
   and prints a summary plus an HTML report under `coverage/` (git-ignored). It is not a gate yet:
   the baseline is recorded first, and a floor is a later decision.
5. **Amending ADR-073.** `printWidth` is 100, the width the code is actually written at, and
   `pnpm format:check` exists; wiring it into the gate waits for one dedicated reformat commit.
   oxlint now runs the jsx-a11y plugin's rules as errors, except `no-autofocus`,
   `prefer-tag-over-role` and `control-has-associated-label` (the config says why for each).

## Consequences

- Four dev dependencies, each with a HANDOFF §3 row, all exact pins in the catalog.
- A new component interaction is tested by role and keyboard, not by markup strings; the static
  markup tests stay where they prove what a surface shows.
- Playwright smoke flows against `prismoid up` remain a separate, later step.
