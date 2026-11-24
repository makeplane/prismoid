# Adding a resource to the operator console - the recipe

The console grows by RESOURCE: a backend read surface, a list page, usually a detail page.
The list page is a `ResourceList` config, not a new component - walk this checklist and the
next resource is an afternoon, not an architecture.

## The anatomy (one resource, end to end)

**Backend** (one commit, its own gates):

1. A fat-model page query in `prismoid/operations/admin_ops.py` (keyset cursor, newest
   first) plus a detail fetch. A detail that exposes CONTENT (a thread, instructions) calls
   `audit()` - see the audit rules below.
2. A typed serializer pair in `prismoid/api/serializers/admin_<resource>.py`
   (`Admin<Resource>Out` for list rows, `Admin<Resource>DetailOut` for the detail page).
3. An endpoint pair in `prismoid/api/endpoints/admin_<resource>.py` (list returns
   `PageOut`, `q`/filters exactly what the query supports) and a row in `api/urls.py`.
4. Regenerate the contract the web builds against: `cd web && pnpm gen`.

**Web** (this directory + two route files):

5. A `ResourceListConfig<T>` + route file in `app/routes/admin/<resource>/route.tsx` -
   copy `app/routes/admin/users/route.tsx`, the reference config.
6. A detail page in `app/routes/admin/<resource>.$id/route.tsx` when rows have depth -
   `clientLoader` + `useLoaderData<typeof clientLoader>` + a `handle.crumb` naming the row,
   and `const [tab, setTab] = useUrlTab(TABS)` for the tab strip (`~/lib/url-state` - the
   first tab is the default and is written as the ABSENCE of `?tab=`). Copy
   `app/routes/admin/projects.$id/route.tsx` (read-only) or
   `app/routes/admin/users.$id/route.tsx` (with verbs). Admin route files never import
   `./+types/route` - use `ClientLoaderFunctionArgs`.
7. One line each in `app/routes.admin.ts` (under the container layout) and the `NAV` array
   in `app/routes/admin-shell/route.tsx`.

## What the engine gives you for free

`ResourceList` (`app/components/admin/resource-list.tsx`) owns the whole list-page anatomy;
the config states only what is resource-shaped:

- **URL-borne state** - `?q=` plus one param per filter group, replace-set, shareable,
  composable with a host page's own params.
- **Search** - debounced (250ms), projected into the shell topbar (`search.topbar`) for a
  page's standing search or inline for embedded copies.
- **Filters** - single-select `ToggleChip` groups with an All default; the chip row only
  renders when filters exist. Ship exactly what the wire supports - never fake a dimension
  the API can't filter.
- **Keyset paging** - `useKeysetList` wiring (`app/lib/paging.ts`), `LoadMore`, optional
  head-refresh polling via `refreshMs`, and the same re-read on demand: `row(item, { refresh })`
  hands it to an inline verb whose door answers no row (the routine pause).
- **States** - `LoadingState`, `EmptyState` (the search-miss variant names the query).
- **Row chrome** - the `Rows` list, hover wash, and the link (`rowTo`) or action (`onRow`)
  wrapper; the config's `row` renders only the content between them. The config type is a
  union: exactly one of the two, enforced by the compiler rather than by this sentence.

## The shared parts (reach for these before writing one)

- `confirm.tsx` - `ConfirmAction` (a guarded verb) and `ReasonDialog` (a guarded verb that
  takes PROSE, because its reason is the substance of the audit row). Suspension and task
  cancellation are both the second one; if your verb writes a reason, it is too.
- `access-tab.tsx` · `audit-rows.tsx` · `task-console.tsx` · `sparkline.tsx` - the detail page
  tabs that are the same everywhere. A `<resource>.$id` page composes these; it does not
  reimplement them.
- `~/lib/url-state` - `useUrlParam` / `useUrlParams` (write params, one history entry, empty
  value deletes) and `useUrlTab`. Never hand-roll a `setSearchParams` block: the copies drift
  on whether they push or replace, and Back stops meaning one thing across the console.
- `~/lib/paging` - `useKeysetList`, and `~/components/kit` for every visual primitive.

## Never hand-keep a server enum

The Models screen kept its own copy of the capability classes. The copy was missing
`image-gen` - a live lane with three catalog models behind it - so that class could be neither
tagged on a new model nor seen in the routing table, and nothing failed to say so. Vocabularies
that live in a Python enum cross the wire (`RoutingOut.classes` ← `CapabilityClass`) and the
screen renders what it is told. If you find yourself typing a list the backend already knows,
add it to a response instead.

## When to go bespoke instead

The engine is for the COMMON shape: fetch → filter → rows → detail. `TaskConsole`
(`task-console.tsx`) stays hand-built on purpose, and it is the example to measure against:

- its rows PEEK in place (`?task=` pushed so Back closes, an overlay button with a nested
  owner link re-raised above it - under `rowTo` the engine's Link wrapper makes nested
  anchors invalid HTML);
- it carries link-borne scope chips (`user_id`/`project_id` resolved to an email) plus hard
  scope props (`userId`/`projectId`) that hide them;
- its status filter is multi-select and rides alongside a boolean (`stuck`).

If your list needs any of that, write the component; if it needs one of those, first ask
whether it really does. `AuditRows` (`audit-rows.tsx`) is the other bespoke case: no
search, no filters from its hosts, subject-scoped by props.

## The audit rules (what gets recorded, and where the UI says so)

- **Mutations: always.** Every admin verb writes an `AdminAction` row in the same
  transaction (`admin_ops.audit()`); reasoned verbs (suspend, cancel) carry the reason.
- **Content-reads: audited.** Reading what a user WROTE is an act - the task peek
  (`task.view`) and a bot's instructions (`bot.view`, the detail GET behind
  `app/routes/admin/bots.$id/route.tsx`). The surface says so quietly ("… is recorded in
  the audit trail"), and the read happens server-side on the detail fetch - the web never
  decides what is audited.
- **Metadata lists: not audited.** Names, counts, statuses, spend figures - the list
  endpoints - are operational observation, not content access.

Subjects the console has a page for become links in the trail (`audit-rows.tsx` ·
`Subject`): user, task (the peek), project, bot. Extend that switch when a new resource
lands.
