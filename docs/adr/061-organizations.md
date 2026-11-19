# ADR-061: Organizations - the tenant

*Status: accepted · 2026-09-29 · supersedes ADR-014 · amended by ADR-062 (2026-09-30)*

## Context

ADR-014 deferred a tenant layer above Spaces, gated on a trigger: the day the product serves
unrelated teams on one deployment. That trigger has fired - Percy serves unrelated teams, and
per-person surfaces (memories, connectors, egress allowances, inbox) can no longer be scoped by
a person alone. Phase 0b makes the Organization the tenant.

## Decision

| Question | Decision |
|---|---|
| Name of the tenant | **Organization.** Spaces stay Spaces. "Workspace" is not used for anything in Percy, so users arriving from Plane (where a workspace is the tenant) are never confused; the Phase 1 sandbox is the **Machine**, one per task (both specs renamed 2026-09-29; this row said "session sandbox" until ADR-062 made the Space the collaboration unit and dropped the session, 2026-09-30). |
| Membership model | A person belongs to many organizations and works in one at a time (the session's active org). Roles: owner, admin, member, guest. |
| How the org reaches queries | **The unit of work.** Every door binds the org to the database session; one helper reads it and fails closed when nothing is bound. Fetchers and tenant-root list queries add `org_id = bound org`. The ~300 `actor: Actor` signatures do not change. Postgres row-level security is a later hardening on top (Phase 5), one `after_begin` hook away. |
| Per-person surfaces | **Per organization.** Memories, connectors and their tokens, egress allowances, inbox, captures and open gates all carry `org_id`. Nothing a person holds in one org is visible or usable from another. |
| Registries | Skills are org-private; seeded builtins are platform rows (`org_id` NULL). An org cannot publish under a builtin's name. Bots gain `org_id`. Bot templates stay a cross-org share link by design; an install lands in the installer's active org. `model_catalog`, `price_book`, `routing_table` stay platform-global. |
| Signup | Creates a personal organization owned by the new person. An invite code also joins the inviting org and makes it active. The first signup on a deployment still gets the platform `Scope.ADMIN`. |
| Admin | Two layers. The platform operator keeps `Scope.ADMIN` and the 36 `/admin` routes (cross-org by design). Org owners and admins manage people and settings through `/v1/orgs/...`, never through `/admin`. Org roles grant no access to other people's Spaces. |
| Usage | Metered in native units and US dollars on `usage_events`, recorded and shown, so the meter stays one table. Nothing here limits a workspace's spend. |
| Valkey hub | **Deferred with a trigger.** The hub already fans out across API replicas: each process holds one Postgres `LISTEN` and `NOTIFY` reaches all of them. No new dependency in 0b. |
| `plane_links` and the plan multiplier | Phase 3, with the Plane integration they serve. |
| Machine time | Phase 1, when the `machines` table exists; metered on `usage_events` like any other spend. |

Also decided:

- **The unit-of-work carrier.** `computer/db/models/tenant.py` holds the binding in the session's
  `info`: `bind`, `bind_platform`, `org_of` (fails closed with `TenantUnbound`), `role_of`,
  `is_platform`, `bound_org`. `session_scope(org_id, platform=)` binds outside a request.
- **The class map is a standing rule.** `db/models/tenancy.py` classifies every table (tenant
  root, ledger stamp, platform-or-org, tenant child, platform, identity); an unclassified table
  fails the build.
- **Valkey stays deferred, with this trigger.** `api/hub.py` holds one `LISTEN` connection per API
  process and fans notifications out to that process's subscribers; `NOTIFY` reaches every
  listening process, so several API replicas already serve one task or conversation. Revisit when
  Lane 2 carries presence and screen traffic (ADR-062 and ADR-065) **and** a measurement crosses a
  budget - notify to subscriber p95 over its §15 budget, or `pg_notification_queue_usage()` above
  10 % under load. The swap then replaces the hub's internals and, as its docstring promises, not
  one emitter.

## Consequences

- Every new table must be classified in `tenancy.py`, or the build fails.
- Every new door (route family, job entry point, webhook) must bind the unit of work; a forgotten
  binding raises `TenantUnbound` (a 500, never a leak).
- References never cross organizations (composite `(space_id, org_id)` FKs).
- Postgres row-level security is the Phase 5 hardening, layered on the same binding.

## Amendment (2026-10-06): the upgrade is an expand; the contract ships later

The first shape of the org revisions stamped history with UPDATEs in the one transaction
`alembic upgrade head` runs: every `task_events` and `usage_events` row rewritten, every tenant
table ACCESS EXCLUSIVE until the commit (about 36 s per 1M+1M ledger rows, over 11 minutes at
10M+10M), and a live writer deadlocked it. It is now expand/contract:

- **Expand (this release).** `org_columns` inserts the default org first and adds every `org_id`
  (and `auth_sessions.active_org_id`) carrying that org's id as a constant column DEFAULT -
  Postgres's fast default, a catalog entry, no row rewritten - NOT NULL on the tenant roots and
  `task_events`. Foreign keys go in `NOT VALID`; `63e1181ed8c2` validates them (SHARE UPDATE
  EXCLUSIVE) and builds the `org_id` indexes CONCURRENTLY in an autocommit block, every statement
  idempotent so a failed run is simply rerun. Each revision takes its table locks up front,
  children before the tables they reference, waiting at most half `deadlock_timeout` per lock and
  retrying from a savepoint (`db/migrations/online.py`): a live writer can make the upgrade wait
  or retry, never deadlock it. The lock windows hold catalog changes and small-table work only
  (the members, a dozen builtin rows, the unique keys of spaces, connectors and skills, the
  per-org personal keys); nothing in them reads a ledger. Measured with pre-org writers running
  (2026-10-06): writers and readers waited on a lock for under 0.3 s at 1M+1M and at 10M+10M
  ledger rows, with no deadlock; validation and the concurrent builds ran 3 s and 74 s beside
  them. Pre-org code still running after the commit keeps inserting without `org_id` and lands
  in the default org.
- **Contract (a later release, once no pre-org code writes).** `ALTER TABLE ... ALTER COLUMN
  org_id DROP DEFAULT` on every table `org_columns` gave one, and on `auth_sessions.active_org_id`:
  metadata only, and a no-op where no DEFAULT exists. It is not part of this release.
- **Nothing relies on the DEFAULT.** A database that ran the earlier shape has none and keeps that
  shape (the rewritten revisions sit behind its head). Every writer stamps `org_id` itself; no
  model declares a server default (`test_tenancy_map`), and an ORM insert that means NULL
  (platform spend, a builtin, a session with no org yet) sends NULL (`test_org_migration`).
