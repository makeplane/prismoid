/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { FolderKanban, Plus, SlidersHorizontal, X } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";
import { revalidateOnPageChange } from "~/lib/pending-route";
import { EmptyState, LoadMore, Pill, Rows, SearchBox, Section } from "~/components/kit";
import { ProjectIcon } from "~/components/icon-picker";
import { SectionTab } from "~/components/layout/section-tab";
import { TopbarPortal } from "~/components/layout/topbar-slot";
import { Button } from "@makeplane/propel/components/button";
import {
  Dialog,
  DialogActions,
  DialogBody,
  DialogClose,
  DialogCloseGroup,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogHeading,
  DialogMain,
  DialogTitle,
  DialogTrigger,
} from "@makeplane/propel/components/dialog";
import { Icon } from "@makeplane/propel/components/icon";
import { IconButton } from "@makeplane/propel/components/icon-button";
import { InputField } from "@makeplane/propel/components/input-field";
import {
  Menu,
  MenuContent,
  MenuLabel,
  MenuRadioGroup,
  MenuRadioItem,
  MenuTrigger,
} from "@makeplane/propel/components/menu";
import { toastError, api, unwrap } from "~/lib/api/client";
import { ago } from "~/lib/format";
import { useDebouncedValue, useKeysetList } from "~/lib/paging";
import type { components } from "~/lib/api/types.gen";
import type { Route } from "./+types/route";

type ProjectRow = components["schemas"]["ProjectListOut"];

export function meta() {
  return [{ title: "Projects · Prismoid" }];
}

const PAGE = 40;

export async function clientLoader() {
  const page = await unwrap(api.GET("/v1/projects", { params: { query: { limit: PAGE } } }));
  return { items: page.items, cursor: page.next_cursor ?? null };
}
clientLoader.hydrate = true as const;

// A tab, a filter or an open peek rides the query string: the page already holds its data, so
// the router does not hold that click for another fetch.
export const shouldRevalidate = revalidateOnPageChange;

/** Role tones: ownership reads as identity, membership as participation. */
const ROLE_TONE = {
  owner: "info",
  editor: "success",
  viewer: "muted",
} as const;

export default function Projects({ loaderData }: Route.ComponentProps) {
  const [q, setQ] = useState("");
  const dq = useDebouncedValue(q.trim(), 250);
  // Group-by rides the URL like /tasks' (a slice is shareable). Role buckets the LOADED
  // window client-side - the same honesty contract as the task browser's non-status
  // groupings; the wire has no role filter, so no Filter menu is faked (list
  // pages: a page adopts only the controls its wire supports).
  const [searchParams, setSearchParams] = useSearchParams();
  const groupByRole = searchParams.get("group") === "role";
  const setGroupByRole = (on: boolean) =>
    setSearchParams(
      (p) => {
        const n = new URLSearchParams(p);
        if (on) n.set("group", "role");
        else n.delete("group");
        return n;
      },
      { replace: true },
    );
  // The app-wide keyset behaviour (lib/paging) - this page was the last hand-rolled copy
  // of debounce+cursor state. Loader seeds page one for the empty query.
  const list = useKeysetList<ProjectRow>({
    fetchPage: (cursor) =>
      unwrap(
        api.GET("/v1/projects", {
          params: {
            query: {
              limit: PAGE,
              ...(dq ? { q: dq } : {}),
              ...(cursor ? { cursor } : {}),
            },
          },
        }),
      ),
    deps: [dq],
    orderKey: (s) => s.created_at,
    initial: dq === "" ? { items: loaderData.items, next_cursor: loaderData.cursor } : undefined,
  });
  const items = list.items ?? [];

  const renderRow = (s: ProjectRow) => (
    <li key={s.id}>
      <Link
        to={`/projects/${s.id}`}
        className="hover:bg-accent/50 flex items-center gap-3 px-4 py-3 transition-colors"
      >
        <ProjectIcon icon={s.icon} hue={s.icon_hue} />
        <span className="min-w-0 flex-1">
          <span className="block truncate text-body-sm-regular font-medium">{s.name}</span>
          <span className="text-muted-foreground text-caption-md-regular">
            created {ago(s.created_at)}
          </span>
        </span>
        <Pill tone={ROLE_TONE[s.role as keyof typeof ROLE_TONE] ?? "muted"} className="capitalize">
          {s.role}
        </Pill>
      </Link>
    </li>
  );

  // Role buckets over the loaded window, ownership first - the tally is the shown rows
  // (plain number; the Load-more tail is the "there may be more" signal, the count law).
  const roleGroups = groupByRole
    ? (["owner", "editor", "viewer"] as const)
        .map((role) => ({ role, rows: items.filter((s) => s.role === role) }))
        .filter((g) => g.rows.length > 0)
    : null;

  return (
    // No in-page title: the breadcrumb IS the page's name - every standing control rides
    // the topbar (search · Display · + New, /tasks' grammar); SectionTab renders no row
    // until this page has chips or a selection to show in it.
    <SectionTab>
      <TopbarPortal>
        <SearchBox value={q} onChange={setQ} placeholder="Search projects…" className="w-56" />
        <Menu>
          <MenuTrigger
            render={
              <IconButton
                // A grouping in force shows as a filled button (was a brighter icon).
                variant={groupByRole ? "tertiary" : "ghost"}
                size="sm"
                icon={<Icon icon={SlidersHorizontal} />}
                aria-label="Display options"
              />
            }
          />
          <MenuContent align="end">
            <MenuRadioGroup
              value={groupByRole ? "role" : "none"}
              onValueChange={(v) => setGroupByRole(v === "role")}
            >
              <MenuLabel>Group by</MenuLabel>
              <MenuRadioItem value="none" label="None" />
              <MenuRadioItem value="role" label="Role" />
            </MenuRadioGroup>
          </MenuContent>
        </Menu>
        <CreateProject />
      </TopbarPortal>

      {items.length === 0 ? (
        <EmptyState
          icon={FolderKanban}
          title={dq ? `No matches for “${dq}”` : "No projects yet"}
          description={dq ? undefined : "Create one and invite the people the work belongs with."}
        />
      ) : roleGroups ? (
        <>
          {roleGroups.map((g) => (
            <Section
              key={g.role}
              title={`${g.role}s`}
              actions={
                <span
                  className="text-muted-foreground text-caption-md-regular tabular-nums"
                  title={
                    list.hasMore
                      ? "Counted from the projects loaded so far - Load more may grow it"
                      : undefined
                  }
                >
                  {g.rows.length}
                </span>
              }
            >
              <Rows as="ul">{g.rows.map(renderRow)}</Rows>
            </Section>
          ))}
          <LoadMore
            hasMore={list.hasMore}
            busy={list.loadingMore}
            onClick={() => void list.loadMore()}
          />
        </>
      ) : (
        <div className="space-y-3">
          <Rows as="ul">{items.map(renderRow)}</Rows>
          <LoadMore
            hasMore={list.hasMore}
            busy={list.loadingMore}
            onClick={() => void list.loadMore()}
          />
        </div>
      )}
    </SectionTab>
  );
}

/** Creation asks one question: the name. Budget and sharing are the project's settings, not
 *  its birth certificate - collaboration first, accounting later. */
function CreateProject() {
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);

  async function create() {
    if (!name.trim()) return;
    setBusy(true);
    try {
      const project = await unwrap(api.POST("/v1/projects", { body: { name: name.trim() } }));
      setOpen(false);
      navigate(`/projects/${project.id}`);
    } catch (e) {
      toastError(e, "Couldn't create the project.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger
        render={
          <Button
            variant="primary"
            size="sm"
            stretch="auto"
            icon={<Icon icon={Plus} />}
            label="New project"
          />
        }
      />
      <DialogContent size="xs">
        <DialogMain>
          <DialogHeader>
            <DialogHeading>
              <DialogTitle>New project</DialogTitle>
              <DialogDescription>
                A shared folder for a team's work. Invite people and set guardrails once it exists.
              </DialogDescription>
            </DialogHeading>
          </DialogHeader>
          <DialogBody>
            <InputField
              size="lg"
              orientation="vertical"
              label="Name"
              value={name}
              onChange={(e) => setName(e.currentTarget.value)}
              placeholder="Research"
              autoFocus
              onKeyDown={(e) => {
                if (e.key === "Enter") void create();
              }}
            />
          </DialogBody>
        </DialogMain>
        <DialogActions>
          <Button
            variant="primary"
            size="md"
            stretch="auto"
            label="Create project"
            onClick={() => void create()}
            disabled={busy || !name.trim()}
          />
        </DialogActions>
        <DialogCloseGroup>
          <IconButton
            variant="ghost"
            size="xs"
            aria-label="Close"
            icon={<Icon icon={X} />}
            render={<DialogClose />}
          />
        </DialogCloseGroup>
      </DialogContent>
    </Dialog>
  );
}

// A failed loader (unwrap throws) shows the friendly boundary in the content area with the
// shell intact, not a whole-app crash - same pattern as the task/project detail routes.
export { RouteErrorBoundary as ErrorBoundary } from "~/components/layout/route-error";
