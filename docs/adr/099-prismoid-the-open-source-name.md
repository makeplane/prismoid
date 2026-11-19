# ADR-099 · Prismoid, the open-source name: Poem becomes Prismoid everywhere

**Status:** accepted · 2026-11-17 · the owner's decisions of 2026-11-16. **Supersedes** ADR-067's
naming row (`poem.*`, the `poem` CLI, `POEM_*`) and HANDOFF §2 rule 11's namespace and prefix.
**Replaces** an unmerged proposal (ADR-098, settings named `APP_*` with the vendors' own
key names).

## Context

The product is being rebranded and its code is about to be published. A reader of the public
repository should meet one name. There are no outside users yet, so the break can be clean, as
ADR-067's was. Unlike ADR-067, existing databases hold data worth keeping (the development
deployments), so the database part is a rename in place, not a fresh baseline.

## Decision

1. **Everything renames**, by a committed script: copy, code identifiers, the Python package (`poem` → `prismoid`), the CLI (`prismoid`,
   the only entry point), paths (`~/.prismoid`, `prismoid up`), the protocol header
   (`X-Prismoid-Protocol`, now **3**), flags (`prismoid:fleet`), the bot namespace
   (`so.plane.prismoid`), database object names, image names the code owns, living docs, and the
   bodies of existing ADRs, STATE.md and HANDOFF.md (the one exception to "records are not rewritten":
   they ship publicly).
2. **The env prefix is `PRISMOID_`, and only that.** `APP_` was the alternative: a neutral
   prefix survives a rename, but it is generic enough to collide with other software in the same
   environment. Provider keys stay under the prefix (`PRISMOID_WORKER_ANTHROPIC_API_KEY`,
   `PRISMOID_GATEWAY_<VENDOR>_API_KEY`) for the same reason: a bare `ANTHROPIC_API_KEY` is a developer's
   own shell variable. There is no fallback reader for the old names.
3. **Migrations are edited in place.** The baseline's revision id becomes `prismoid_baseline`, its
   trigger function `prismoid_append_only()`, and the NOTIFY channel (a Python constant)
   `prismoid_events`. Alembic stores only the head revision, which carries no product name, so a
   database past the baseline keeps upgrading. A database created before this decision runs one
   statement, once, before its first boot on this build:

   ```sql
   ALTER FUNCTION poem_append_only() RENAME TO prismoid_append_only;
   ```

   Triggers reference the function by OID and keep firing. A deployment's database URL keeps naming
   whatever role and database it already has; only the dev defaults say `prismoid`.
4. **The sandbar Go module is `github.com/makeplane/prismoid/sandbar`**, the path it lives at in the
   public repository, so `go install` and `go get` resolve it.

## What keeps its old name

- This record, which names the old ones.
- Everything ADR-067 kept but the sandbar Go module (point 4): computer use (the feature),
  `SANDBAR_*`, the token prefixes.

## Consequences

- Every deployment's environment switches from `POEM_*` (or the unmerged proposal's `APP_*`) to `PRISMOID_*` in
  lockstep with the first image built from this decision. A deployment that still sets only the old
  names boots with the dev defaults and fails at its first database connection.
- A Mac running the local mode moves `~/.poem` to `~/.prismoid` and renames its config keys.
