# ADR-014 — Workspace tenancy: deferred, with the retrofit path recorded

*Status: superseded by ADR-061 (2026-09-29)*

## Context

The question: should a **workspace** layer sit between accounts and Spaces — Notion/Linear-style
tenants, where a user belongs to multiple workspaces, everything inside a workspace is isolated,
and a `workspace_id` FK traverses the whole schema?

Today the answer the architecture gives is: **the deployment is the workspace.** The product is
sovereign single-instance (ADR-012); one install serves one household of users; the Settings UI
already uses "workspace" to mean *this installation*. ADR-009's deploy-target row states the
posture and its expiry in one line: "Compose remains the deploy unit through beta. **Trigger: the
first user who is not the owner.**" There is no org/tenant/team entity anywhere in the 27 tables,
and no route under `/v1/orgs` or equivalent. Spaces are deliberately project-simple folders
(HANDOFF §6) — membership and budgets, nothing more — not tenants.

This ADR records the assessment made when the question was asked, so the decision can be
re-opened later without re-deriving it.

## Assessment

### What makes the retrofit unusually cheap here

- **All SQL lives in `backend/computer/db/models/`** (import-linter + `just audit` enforced; ~71
  `select()` calls in 24 files). A tenant predicate has a small, closed set of legal insertion
  points — no query hunt across the ~180 operations functions.
- **Two fetch paths carry essentially all space/task authorization**: `Space.get_for(min_role)`
  (`db/models/space.py`) and `Task.get_for` / `Task._visible_to` (`db/models/task.py`). Their
  ~38 call sites inherit whatever those two functions enforce.
- **The denormalized-scope precedent exists.** `task_events.space_id` and `usage_events.space_id`
  are stamped at write time under the ledger law (a move never rewrites history). A
  `workspace_id` column follows the same documented rule.
- **The schema safety net.** `ALL_TABLES` in `db/models/__init__.py` is diffed against the live
  database by `tests/integration/test_schema.py`; a forgotten table is a failing test.

### What makes it genuinely expensive — five undecided product questions

1. **Auth has no tenant context.** `require_actor` resolves a bearer token to a bare `Actor` in
   one query — a hot path under §15's budgets, including the SSE `?token=` concession. Real
   isolation needs workspace-scoped sessions/API keys (schema + contract break) or a workspace
   header plus a membership check on every request.
2. **Four registries are global by unique constraint**: `skills_registry (name, version)`,
   `model_catalog (provider, model_id)`, `routing_table (version, capability_class)`,
   `price_book (version, provider, model, unit)`. Two workspaces cannot both own a `report`
   skill today; `head(name)`, `published_heads()`, and `resolve_ref()` assume one namespace.
   Each registry needs an explicit per-workspace vs platform-global decision; skills is the
   hardest.
3. **Spaceless tasks.** `tasks.space_id` is nullable by design ("created out of thin air, moved
   freely"). A real tenant boundary wants `workspace_id NOT NULL` on tasks, which re-opens the
   personal-task semantics — `_visible_to`'s `creator_actor_id == actor.id` clause would
   otherwise leak a person's tasks across their workspaces.
4. **Admin is system-global**, bootstrapped by first signup (`User.signup(first=True)` →
   `Scope.ADMIN`). Multi-tenant needs per-workspace roles — likely a `workspace_members` table
   mirroring `space_members` — and a rework of `require_admin` and the 7 `/admin/*` routes.
5. **Per-actor surfaces bleed.** `inbox_items`, `captures`, and `Gate.open_for` are actor-only;
   user-scope memories are doctrinally "follow the person across every space." Whether private
   memories, inbox, and open gates follow a person *between workspaces* is policy, not schema —
   and it is unanswered.

## Decision

**Defer.** No workspace entity, no tenant column, no scaffolding now — §14's "no Phase-2+
scaffolding beyond named stubs" applies. The single-instance posture is load-bearing and
correct for the current product.

**Trigger to revisit:** a hosted deployment serving a second, unrelated customer on one
instance — or ADR-009's deploy-target trigger firing (the first user who is not the owner)
with a hosted (not self-hosted) rollout planned.

When the trigger fires, the recorded path is staged so isolation cost is paid only when needed:

- **Phase A — scaffold (behavior unchanged).** `workspaces` + `workspace_members` tables;
  `workspace_id NOT NULL` stamped on the work tables (tasks, plan_*, task_events, gates,
  artifacts, captures, inbox_items, usage ledger + rollup, memories, features, spaces),
  denormalized like the ledger; every deployment gets a single default workspace created by
  migration; the predicate threads through `Space.get_for` and `Task._visible_to` only. One
  workspace ⇒ every query returns what it returns today.
- **Phase B — isolation.** Workspace-scoped sessions and API keys, per-workspace admin roles
  replacing the global scope for non-platform concerns, the workspace switcher in the shell,
  and explicit answers for the per-actor surfaces (inbox/captures/gates filter by active
  workspace; user memories get a workspace-follows policy).
- **Phase C — registries.** Per-registry decision: platform-global with per-workspace
  enablement (likely right for `model_catalog`, `price_book`, `routing_table`) vs
  workspace-namespaced (`skills_registry`, which needs the unique key reworked around a tenant
  column or a partial index).

## Consequences

- Nothing is built, so nothing taxes today's queries, budgets (§15), or the frozen API
  contract.
- **The retrofit grows with the schema.** Every new table, every new global-unique constraint,
  and every new per-actor surface added before Phase A widens it. Until this ADR is superseded:
  design new global registries and per-actor surfaces with a future tenant column in mind
  (avoid new NULL-means-global conventions; prefer scope columns that can gain one more lane).
- The exploration that priced this decision (fetch paths, blast radius ≈56 files touching
  `space_id`, the five questions above) is captured here; re-opening the question starts from
  this document, not from zero.
