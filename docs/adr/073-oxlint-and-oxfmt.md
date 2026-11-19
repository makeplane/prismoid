# ADR-073 · oxlint and oxfmt replace ESLint and Prettier

**Status:** accepted · 2026-10-30 · the owner's ask of 2026-10-30 ("replace eslint and prettier with
oxlint and oxfmt"), matching Plane's toolchain. **Amends** ADR-009 (the web half of the gate
lints with oxlint, not ESLint).

## Context

The web app linted with ESLint 10 (`@eslint/js` recommended, typescript-eslint recommended, two
rules from eslint-plugin-react-hooks, and the dnd-kit import fence of ADR-016) and formatted with
Prettier 3 on its defaults. `prettier-plugin-tailwindcss` was installed but never configured, so it
never ran. Formatting was never gated: 244 files already differed from Prettier's output. That is
six packages and a Node-hosted config for one lint pass and one manual format command. Plane
runs both jobs on the Oxc tools.

## Decision

1. **oxlint lints** (`pnpm lint` = `oxlint app`, a turbo task in the gate). `web/.oxlintrc.json`
   was generated from the ESLint config by `@oxlint/migrate`, not rewritten by hand: the same 86
   rules at the same severities, the `argsIgnorePattern: "^_"`, the TypeScript overrides, the
   ignores, and the dnd-kit fence with its collection-editor exemption (verified: an `@dnd-kit`
   import outside the collection fails the gate with ADR-016's message). Four rules were not
   carried: `no-undef` and `no-useless-assignment` are still experimental in oxlint, and
   `no-dupe-args` and `no-octal` will not be implemented. TypeScript and ES modules already reject
   what those four catch.
2. **oxfmt formats** (`pnpm format` = `oxfmt app`). `web/.oxfmtrc.json` pins `printWidth: 80`, the
   one Prettier default oxfmt does not share (its default is 100), and carries `.prettierignore`'s
   patterns (the registry-owned `components/ui/` and the generated `*.gen.ts`). Its output matches
   Prettier's on this code: 245 files differ from oxfmt against 244 from Prettier. Formatting stays
   out of the gate, as before. Gating it is a separate decision, with a separate reformat commit.
3. **ESLint suppressions keep working.** oxlint honours `eslint-disable-next-line` comments. Where
   it reports at a different spot than ESLint did (`useFetch` in `routes/bots/shelves.tsx`, whose
   deps are its caller's list), an `oxlint-disable-next-line` comment sits beside the original.
4. **Removed:** `eslint`, `@eslint/js`, `typescript-eslint`, `eslint-plugin-react-hooks`,
   `prettier`, `prettier-plugin-tailwindcss`, `web/eslint.config.js`, `web/.prettierignore`.
   **Added:** `oxlint`, `oxfmt` (catalog, exact pins, HANDOFF §3 row).

## Consequences

- An editor that formats on save needs the oxc extension (it provides both tools' LSP) in place of
  the ESLint and Prettier ones.
- Type-aware linting (`oxlint --type-aware`, `oxlint-tsgolint`) is available but not enabled. The
  ESLint config had no type-aware rules, so turning it on would be a new policy, not a port.
