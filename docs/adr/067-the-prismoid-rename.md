# ADR-067 · The Prismoid rename: Computer, Organization and Space become Prismoid, Workspace and Project

**Status:** accepted · 2026-10-08 · the owner's decisions of 2026-10-08.
**Supersedes** ADR-061's naming row
("Workspace is not used for anything") and the Space wording of ADR-062 and CLAUDE.md, and HANDOFF
§2 rule 11's `computer.*` namespace and `COMPUTER_` prefix. ADR bodies stay as they were written;
read their Space as Project, their Organization/org as Workspace, `computer.*` as `prismoid.*`.

## Context

The product is Prismoid. Plane's own vocabulary is workspace (the tenant) and project (where people
work together), and our names for the same two things were Organization and Space. Keeping two
names for one thing in code, API and copy was the cost; the product had no external users and no
data worth migrating, so the rename could be total and the break clean.

## Decision

1. **Everything renames.** Copy, API paths and JSON fields, code identifiers, database tables,
   columns and constraints, the Python package (`computer` → `prismoid`), the CLI (`computer` →
   `prismoid`), the env prefix (`COMPUTER_*` → `PRISMOID_*`, `VITE_COMPUTER_*` → `VITE_PRISMOID_*`), the headers
   (`X-Computer-*` → `X-Prismoid-*`), and the infrastructure names the code owns: the Postgres role
   and database `prismoid` (and `prismoid_dbos_sys`), MinIO's dev credentials, the `prismoid_frame_net`
   network, the `prismoid-frame*` images (sandbar's rootfs template default follows), the
   `prismoid_events` NOTIFY channel, the `prismoid_append_only()` trigger function. "Plane Computer" is
   "Prismoid"; Plane the company and the charter's "Plane, the AI factory" stay.

   | Was | Now |
   |---|---|
   | Organization, org, `org_id`, `/v1/orgs`, `OrgRole` | Workspace, workspace, `workspace_id`, `/v1/workspaces`, `WorkspaceRole` |
   | Space, `space_id`, `/v1/spaces`, `space_members` | Project, `project_id`, `/v1/projects`, `project_members` |
   | `computer.*`, `computer` CLI, `COMPUTER_*` | `prismoid.*`, `prismoid` CLI, `PRISMOID_*` |

2. **A clean API break.** No aliases, no compatibility readers: every database is created fresh,
   so no stored event carries an old key. `X-Prismoid-Protocol` is **2**; a client on 1 is told it
   is stale.

3. **One fresh migration baseline.** The 74 revisions are replaced by `prismoid_baseline`, generated
   from the renamed models plus what a model cannot express (the append-only function and its
   triggers). A database stamped with any old revision fails at
   `upgrade` ("Can't locate revision"); it is dropped and recreated, never half-applied. A
   one-off proof renamed a database the old chain built and compared its normalized schema dump
   with the baseline's. The differences it reports, each deliberate:
   - the singleton cursor (`routine_event_cursor`) loses an unused sequence: its model now says
     `autoincrement=False`, which is also what makes `alembic check` clean (it never was on the old
     chain);
   - two foreign keys the old chain named by hand (`fk_tasks_origin_conversation_id`,
     `fk_usage_events_conversation_id`) take the naming convention's names;
   - the memories CHECK reads scope `'project'` where it read `'space'`.

   The proof also found drift the old chain had and the models did not, now declared on the
   models so the baseline keeps it: the partial index `ix_tasks_origin_conversation_id` and seven
   database defaults (`artifacts.revision`, `bot_channels.config`, `bot_template_installs.report`,
   `bot_templates.manifest`, `messages.artifact_ids|facts|meta`).

4. **A re-runnable rename, not hand edits.** A committed stdlib script rewrote every
   identifier-like token segment by segment with its case kept, behind a mask of protected spans,
   and moved paths with the same function. Its tests pinned every collision found (a dry run, the
   live run, and an independent review). Re-running it on a renamed tree changed nothing;
   re-running it after a rebase renamed only what arrived.

## What keeps its name, and why

- **Not ours to rename:** the development repository and its image paths; live cloud resources
  (renaming a document does not rename them); `SANDBAR_*` and the sandbar Go module (the module
  moved later, ADR-099).
- **Formats, not names:** the opaque token prefixes (`cs_ ck_ cr_ cn_ rt_ bc_ si_ st_ dt_`).
- **A feature, not the app:** computer use (`computer_use`, `ComputerUse*`, `COMPUTER_USE*`, the
  navigator); only its env prefix moved (`PRISMOID_COMPUTER_USE_*`). Prose like "your computer".
- **Words that only contain the old ones:** whitespace and Tailwind `space-*`, typography's
  `space_after`, "address space", `organize`, `.org` domains, DigitalOcean Spaces.
- **History:** ADR bodies, HANDOFF §3 onward, and the development records that stay in
  Makeplane's private archive (logs, plans, specs, STATE.md's dated entries, tags m0-m10).

## Consequences

- Every development database is dropped and recreated (the owner's call).
- **The one production deployment** of the pre-rename stack was deleted instead of cut over
  (*amended 2026-10-12*), and its cloud-specific deploy path went with it. The one-box compose
  path stays for self-hosters, deployed by hand with `deploy/deploy.sh`.
- **Dev machines:** the shared dev stack is now compose project `prismoid` with role `prismoid`. A machine
  still running the old `computer` project keeps ports 5433 and 9000, so `just up` fails until
  `docker compose -p computer down`.
- A deployment's own configuration switches env names in lockstep with images built from this tree
  (suggested: render both prefixes for one release, then drop `COMPUTER_*`).
- A change built on the old names lands by rebase, a re-run
  of the script, folding any new revision into the baseline, and a re-run of the proof.
- "Workspace" now names the tenant. The admin console's comments that used it for a detail page
  say "detail page".
