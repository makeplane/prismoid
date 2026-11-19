/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { capitalize } from "~/lib/utils";
import { Brain, FileText, Plus, Trash2, UserPlus, X } from "lucide-react";
import { useCallback, useMemo, useRef, useState } from "react";
import { useRevalidator, useSearchParams } from "react-router";
import { toast } from "@prismoid/blocks/toast";
import { ArtifactReader } from "~/components/artifact/artifact-reader";
import { IconPicker, ProjectIcon } from "~/components/icon-picker";
import { MemoryRowItem, type MemoryRow } from "~/components/memory/memory-row";
import { TaskBrowser } from "~/components/tasks/task-browser";
import {
  artifactIcon,
  artifactKindLabel,
  AvatarStack,
  EmptyState,
  IconTile,
  initials,
  LoadingState,
  LoadMore,
  Money,
  PageHeader,
  Pill,
  Rows,
  SearchBox,
  Section,
  VerdictBadge,
} from "~/components/kit";
import { Badge } from "@makeplane/propel/components/badge";
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
import { Input, InputGroup } from "@makeplane/propel/components/input";
import { InputField } from "@makeplane/propel/components/input-field";
import { Popover, PopoverContent, PopoverTrigger } from "@makeplane/propel/components/popover";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectList,
  SelectTrigger,
} from "@makeplane/propel/components/select";
import { SelectField } from "@makeplane/propel/components/select-field";
import { TextAreaField } from "@makeplane/propel/components/text-area-field";
import { Tab, Tabs, TabsList, TabsPanel } from "@makeplane/propel/components/tabs";
import { useViewer } from "~/hooks/use-viewer";
import { toastError, api, unwrap } from "~/lib/api/client";
import { ago, personName } from "~/lib/format";
import { rememberProjectName } from "~/lib/project-names";
import { useDebouncedValue, useKeysetList } from "~/lib/paging";
import type { components } from "~/lib/api/types.gen";
import { InviteLinks } from "./invite-links";
import { LiveNow } from "./live-now";
import { OwnerRow } from "./owner-row";
import { canStartTask, ProjectComposer } from "./project-composer";
import type { Route } from "./+types/route";

type ArtifactRow = components["schemas"]["ArtifactOut"];
type MemberRow = components["schemas"]["MemberOut"];

export function meta({ loaderData }: Route.MetaArgs) {
  return [
    {
      title: loaderData?.project ? `${loaderData.project.name} · Prismoid` : "Project",
    },
  ];
}

const PAGE = 30;

/** A tab panel's body: the kit's 24px drop under the strip (Propel's set gaps 12px), its xs
 *  type and its ink (Propel's panel is body-sm in secondary ink). */
const PANEL = "mt-3 text-caption-md-regular/relaxed text-foreground";
// The member row's trigger shows the role's name, never the raw wire value.
const ROLE_LABELS = { editor: "Editor", viewer: "Viewer" };
const ROLE_OPTIONS = [
  { value: "editor", label: "Editor", description: "create, run, resolve" },
  { value: "viewer", label: "Viewer", description: "read only" },
];

export async function clientLoader({ params }: Route.ClientLoaderArgs) {
  const path = { project_id: params.id };
  const [project, members, usage] = await Promise.all([
    unwrap(api.GET("/v1/projects/{project_id}", { params: { path } })),
    unwrap(api.GET("/v1/projects/{project_id}/members", { params: { path } })).catch(
      () => [] as MemberRow[],
    ),
    unwrap(api.GET("/v1/projects/{project_id}/usage", { params: { path } })).catch(() => null),
  ]);
  // A task page's crumb names this Project: tell it, so opening a task never re-asks (and a
  // rename, which revalidates this loader, is picked up).
  rememberProjectName(project.id, project.name);
  return { project, members, usage };
}
clientLoader.hydrate = true as const;

// The breadcrumb says WHICH project, not "Detail".
export const handle = {
  crumb: (data: unknown) =>
    (data as { project?: { name?: string } } | undefined)?.project?.name ?? null,
};

/**
 * A project is a room, not a ledger: the header says who's in it, the tabs say what it holds
 * (the work and its artifacts, both searchable and unbounded), and everything about the room
 * itself - people, name, budget, ownership - is Settings. The page rides the standard
 * container, and every tab fills it - one width everywhere, nothing hugging a corner.
 */
export default function ProjectDetail({ loaderData }: Route.ComponentProps) {
  const { project, members, usage } = loaderData;
  const isOwner = project.role === "owner";
  const people = members.length + 1; // + the owner (a column, not a membership row)
  // B3: the budget dock links here with ?tab=settings, where the owner raises the Project's budget
  const [params] = useSearchParams();

  return (
    <div className="space-y-6">
      <PageHeader
        leading={
          <ProjectFace
            key={project.id}
            projectId={project.id}
            icon={project.icon}
            hue={project.icon_hue}
            isOwner={isOwner}
          />
        }
        title={project.name}
        description={
          <span className="text-muted-foreground flex flex-wrap items-center gap-2 text-body-sm-regular">
            <Pill tone={project.role === "owner" ? "info" : "muted"} className="capitalize">
              {project.role}
            </Pill>
            {people} {people === 1 ? "person" : "people"} · created {ago(project.created_at)}
          </span>
        }
        actions={
          <div className="flex items-center gap-3">
            {/* the owner is a column, not a member row: the stack names them first */}
            <AvatarStack
              people={[
                {
                  id: project.owner.user_id,
                  name: personName(project.owner.name || project.owner.email),
                },
                ...members.map((m) => ({
                  id: m.actor_id,
                  name: personName(m.name || m.email),
                })),
              ]}
              label={`${people} ${people === 1 ? "person" : "people"}`}
            />
            {isOwner && <InviteMember projectId={project.id} />}
          </div>
        }
      />

      <Tabs
        variant="contained"
        defaultValue={params.get("tab") === "settings" ? "settings" : "tasks"}
      >
        <TabsList>
          <Tab value="tasks" label="Tasks" />
          <Tab value="artifacts" label="Artifacts" />
          <Tab value="settings" label="Settings" />
        </TabsList>

        <TabsPanel value="tasks">
          <div className={`${PANEL} space-y-6`}>
            {/* A task starts here, scoped to this Project - editors and owners only
                (a viewer is offered no write the server would refuse). */}
            <ProjectComposer project={project} />
            {/* The lobby: who is doing what in this Project right now. Keyed so a
                sidebar hop to another Project never carries the last one's rows. */}
            <LiveNow key={project.id} projectId={project.id} />
            {/* The app-wide task browser, hard-scoped: group-by, filters, List/Board -
                minus the project dimension, which is not a question in here. */}
            <TaskBrowser projectId={project.id} canStart={canStartTask(project.role)} />
          </div>
        </TabsPanel>
        <TabsPanel value="artifacts">
          <div className={PANEL}>
            <ArtifactsTab projectId={project.id} readOnly={project.role === "viewer"} />
          </div>
        </TabsPanel>
        <TabsPanel value="settings">
          <div className={PANEL}>
            <SettingsTab project={project} usage={usage} members={members} isOwner={isOwner} />
          </div>
        </TabsPanel>
      </Tabs>
    </div>
  );
}

/** The project's face in the header - the owner clicks it to change (Perplexity's picker:
 *  icons/emoji tabs, search, the hue row); everyone else just sees it. Optimistic: paint
 *  the pick immediately, PATCH behind it, roll back with a toast on failure. Keyed by
 *  project in the caller so a sidebar hop never carries stale face state. */
function ProjectFace({
  projectId,
  icon,
  hue,
  isOwner,
}: {
  projectId: string;
  icon: string | null;
  hue: number | null;
  isOwner: boolean;
}) {
  const revalidator = useRevalidator();
  const [face, setFace] = useState({ icon, hue });

  async function pick(next: { icon?: string | null; hue?: number | null }) {
    const prev = face;
    setFace({
      icon: next.icon !== undefined ? next.icon : prev.icon,
      hue: next.hue !== undefined ? next.hue : prev.hue,
    });
    // null means "back to default" - the wire says it with clear flags (PATCH can't).
    // The contract requires every clear flag stated; false = leave that field alone.
    const body = {
      clear_budget: false,
      clear_icon: false,
      clear_hue: false,
    } as {
      icon?: string;
      icon_hue?: number;
      clear_budget: boolean;
      clear_icon: boolean;
      clear_hue: boolean;
    };
    if (next.icon !== undefined) {
      if (next.icon === null) body.clear_icon = true;
      else body.icon = next.icon;
    }
    if (next.hue !== undefined) {
      if (next.hue === null) body.clear_hue = true;
      else body.icon_hue = next.hue;
    }
    try {
      const updated = await unwrap(
        api.PATCH("/v1/projects/{project_id}", {
          params: { path: { project_id: projectId } },
          body,
        }),
      );
      // Adopt the SERVER's face, not the optimistic guess: a server that ignored the
      // fields (a stale process was exactly this bug) shows the truth instead of a lie.
      setFace({ icon: updated.icon, hue: updated.icon_hue });
      revalidator.revalidate(); // the list page reads the same rows
    } catch (e) {
      setFace(prev);
      toastError(e, "Couldn't update the icon.");
    }
  }

  if (!isOwner) return <ProjectIcon icon={face.icon} hue={face.hue} size="lg" />;
  return (
    <Popover>
      <PopoverTrigger
        render={
          <button
            type="button"
            aria-label="Change project icon"
            className="hover:ring-foreground/25 focus-visible:ring-ring rounded-lg ring-1 ring-transparent ring-inset outline-hidden transition-shadow focus-visible:ring-2"
          />
        }
      >
        <ProjectIcon icon={face.icon} hue={face.hue} size="lg" />
      </PopoverTrigger>
      <PopoverContent variant="rich" align="start">
        <IconPicker icon={face.icon} hue={face.hue} onPick={(n) => void pick(n)} />
      </PopoverContent>
    </Popover>
  );
}

/** A search box over a keyset list - the tab-body shape both Tasks and Artifacts share.
 *  Pagination itself is the app-wide `useKeysetList`; this adds only the debounced query. */
function useSearchedPage<T extends { id: string; created_at: string }>(
  fetchPage: (
    q: string,
    cursor: string | null,
  ) => Promise<{
    items: T[];
    next_cursor?: string | null;
  }>,
) {
  const [q, setQ] = useState("");
  const dq = useDebouncedValue(q.trim(), 250);
  const fetchRef = useRef(fetchPage);
  fetchRef.current = fetchPage;
  const list = useKeysetList<T>({
    fetchPage: (cursor) => fetchRef.current(dq, cursor),
    deps: [dq],
    orderKey: (item) => item.created_at,
  });
  return { ...list, q, setQ, activeQ: dq };
}

function ArtifactsTab({
  projectId,
  readOnly,
}: {
  projectId: string;
  /** A Project viewer reads the shelf; edits, restores and labels are an editor's. */
  readOnly: boolean;
}) {
  const [peek, setPeek] = useState<string | null>(null);
  const page = useSearchedPage<ArtifactRow>(async (q, cursor) =>
    unwrap(
      api.GET("/v1/projects/{project_id}/artifacts", {
        params: {
          path: { project_id: projectId },
          query: {
            limit: PAGE,
            ...(q ? { q } : {}),
            ...(cursor ? { cursor } : {}),
          },
        },
      }),
    ),
  );

  return (
    <div className="space-y-3">
      <SearchBox value={page.q} onChange={page.setQ} placeholder="Search artifacts…" />
      {page.items === null ? null : page.items.length === 0 ? (
        <EmptyState
          icon={FileText}
          title={page.activeQ ? `No matches for “${page.activeQ}”` : "No artifacts yet"}
          description={
            page.activeQ ? undefined : "Everything the project's tasks publish lands here."
          }
        />
      ) : (
        <>
          <Rows as="ul">
            {page.items.map((a) => (
              <li key={a.id}>
                {/* A row peeks - the drawer holds the preview; full screen is the
                    reader's escalation, never the default jump. */}
                <button
                  onClick={() => setPeek(a.id)}
                  className="hover:bg-accent/50 flex w-full items-center gap-3 px-4 py-3 text-left transition-colors"
                >
                  <IconTile icon={artifactIcon(a.type)} />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-body-sm-regular font-medium">
                      {a.title}
                    </span>
                    <span className="text-muted-foreground text-caption-md-regular">
                      {artifactKindLabel(a.type)} · {ago(a.created_at)}
                    </span>
                  </span>
                  <VerdictBadge verdict={a.verdict} showIcon={false} />
                </button>
              </li>
            ))}
          </Rows>
          <LoadMore
            hasMore={page.hasMore}
            busy={page.loadingMore}
            onClick={() => void page.loadMore()}
          />
        </>
      )}
      {/* The same resizable reader the task page opens - one component, every shelf. */}
      <ArtifactReader
        artifactId={peek}
        onClose={() => setPeek(null)}
        kindHint={page.items?.find((a) => a.id === peek)?.type}
        // A save/restore in the drawer mints a new head - repoint the peek id so
        // kindHint and a reopen track the lineage's face, same as the task page hosts.
        onArtifactChanged={setPeek}
        readOnly={readOnly}
      />
    </div>
  );
}

/** The roster, as a Settings section - who's in the Project is a property of the Project. */
function PeopleSection({
  projectId,
  owner,
  members,
  isOwner,
}: {
  projectId: string;
  owner: { name: string; email: string | null }; // email null: a guest's read
  members: MemberRow[];
  isOwner: boolean;
}) {
  const revalidator = useRevalidator();

  async function setRole(actorId: string, role: string) {
    try {
      await unwrap(
        api.PATCH("/v1/projects/{project_id}/members/{actor_id}", {
          params: { path: { project_id: projectId, actor_id: actorId } },
          body: { role },
        }),
      );
      revalidator.revalidate();
    } catch (e) {
      toastError(e, "Couldn't change the role.");
    }
  }

  async function remove(actorId: string) {
    try {
      await unwrap(
        api.DELETE("/v1/projects/{project_id}/members/{actor_id}", {
          params: { path: { project_id: projectId, actor_id: actorId } },
        }),
      );
      revalidator.revalidate();
    } catch (e) {
      toastError(e, "Couldn't remove the member.");
    }
  }

  // The owner leads the roster: they are a column, not a member row, so the members list alone
  // would never name who owns the Project.
  const roster = (
    <div className="space-y-2">
      <Rows as="ul">
        <OwnerRow owner={owner} />
        {members.map((m) => (
          <li key={m.actor_id} className="flex items-center gap-3 px-4 py-3">
            <span className="bg-muted text-muted-foreground flex size-8 shrink-0 items-center justify-center rounded-full text-caption-md-regular font-medium">
              {initials(personName(m.name || m.email))}
            </span>
            <span className="min-w-0 flex-1">
              <span className="block truncate text-body-sm-regular font-medium">
                {m.name || m.email || m.actor_id}
              </span>
              {m.email && (
                <span className="text-muted-foreground block truncate text-caption-md-regular">
                  {m.email}
                </span>
              )}
            </span>
            {isOwner ? (
              <span className="flex shrink-0 items-center gap-1">
                <Select
                  modal={false}
                  value={m.role}
                  onValueChange={(v) => v && void setRole(m.actor_id, v)}
                  items={ROLE_LABELS}
                >
                  <SelectTrigger size="md" variant="secondary" width="fit" aria-label="Role" />
                  <SelectContent align="end">
                    <SelectList>
                      <SelectItem value="editor" label="Editor" />
                      <SelectItem value="viewer" label="Viewer" />
                    </SelectList>
                  </SelectContent>
                </Select>
                <IconButton
                  variant="ghost"
                  size="sm"
                  icon={<Icon icon={Trash2} />}
                  aria-label={`Remove ${personName(m.name || m.email)}`}
                  onClick={() => void remove(m.actor_id)}
                />
              </span>
            ) : (
              <Badge variant="neutral" size="xs" label={capitalize(m.role)} />
            )}
          </li>
        ))}
      </Rows>
      {members.length === 0 && isOwner && (
        <p className="text-muted-foreground text-caption-md-regular">
          Just you so far. Invite a teammate - they'll see this project's tasks and artifacts.
        </p>
      )}
    </div>
  );

  return (
    <>
      <Section
        title="People"
        actions={isOwner ? <InviteMember projectId={projectId} /> : undefined}
      >
        {roster}
      </Section>
      {isOwner && <InviteLinks projectId={projectId} />}
    </>
  );
}

// ── Settings: the owner's drawer - naming, accounting, ownership ─────────────────────────────

/** A setting is a row: what it is on the left, the control on the right. Save appears only
 *  when there's something to save - a resting settings page has no buttons to ignore. */
function SettingRow({
  label,
  hint,
  children,
}: {
  label: string;
  hint: string;
  children: React.ReactNode;
}) {
  return (
    <li className="flex flex-wrap items-center justify-between gap-x-6 gap-y-2 px-4 py-3.5">
      <span className="min-w-0">
        <span className="block text-body-sm-regular font-medium">{label}</span>
        <span className="text-muted-foreground block text-caption-md-regular">{hint}</span>
      </span>
      <span className="flex shrink-0 items-center gap-2">{children}</span>
    </li>
  );
}

function SettingsTab({
  project,
  usage,
  members,
  isOwner,
}: {
  project: Route.ComponentProps["loaderData"]["project"];
  usage: Route.ComponentProps["loaderData"]["usage"];
  members: MemberRow[];
  isOwner: boolean;
}) {
  const revalidator = useRevalidator();
  const [name, setName] = useState(project.name);
  const [budget, setBudget] = useState(project.budget_usd ?? "");
  const [busy, setBusy] = useState(false);
  // The usage route serializes Decimals as strings inside an untyped payload (dict[str, Any]).
  const spent = (usage?.month_to_date_usd as string | undefined) ?? null;

  const nameDirty = name.trim() !== "" && name.trim() !== project.name;
  // Numeric compare: the wire says "25.0000" where the owner typed "25" - same ceiling.
  const asNum = (v: string | null | undefined) => (v == null || v.trim() === "" ? null : Number(v));
  const budgetDirty = asNum(budget) !== asNum(project.budget_usd);

  async function save(body: { name?: string; budget_usd?: string; clear_budget?: boolean }) {
    setBusy(true);
    try {
      const updated = await unwrap(
        api.PATCH("/v1/projects/{project_id}", {
          params: { path: { project_id: project.id } },
          body: {
            ...body,
            clear_budget: body.clear_budget ?? false,
            clear_icon: false,
            clear_hue: false,
          },
        }),
      );
      // Echo the canonical values back ("25" saved as "25.0000" must not stay dirty).
      setName(updated.name);
      setBudget(updated.budget_usd ?? "");
      revalidator.revalidate();
      toast.success("Saved.");
    } catch (e) {
      toastError(e, "Couldn't save that.");
    } finally {
      setBusy(false);
    }
  }

  const saveName = () => nameDirty && void save({ name: name.trim() });
  const saveBudget = () =>
    budgetDirty &&
    void save(budget.trim() ? { budget_usd: budget.trim() } : { clear_budget: true });

  return (
    <div className="space-y-8">
      <PeopleSection
        projectId={project.id}
        owner={project.owner}
        members={members}
        isOwner={isOwner}
      />

      <ContextSection project={project} members={members} isOwner={isOwner} />

      {!isOwner ? null : (
        <>
          <Section title="General">
            <Rows as="ul">
              <SettingRow label="Name" hint="How this project appears everywhere.">
                <div className="w-56">
                  <InputGroup size="lg">
                    <Input
                      size="lg"
                      value={name}
                      onChange={(e) => setName(e.currentTarget.value)}
                      onKeyDown={(e) => e.key === "Enter" && saveName()}
                      aria-label="Project name"
                    />
                  </InputGroup>
                </div>
                {nameDirty && (
                  <Button
                    size="sm"
                    stretch="auto"
                    variant="secondary"
                    label="Save"
                    disabled={busy}
                    onClick={saveName}
                  />
                )}
              </SettingRow>
            </Rows>
          </Section>

          <Section title="Budget">
            <Rows as="ul">
              <SettingRow label="Monthly ceiling" hint="In USD. Past it, no new work starts here.">
                <div className="w-28">
                  <InputGroup size="lg">
                    <Input
                      size="lg"
                      inputMode="decimal"
                      value={budget}
                      onChange={(e) => setBudget(e.currentTarget.value)}
                      onKeyDown={(e) => e.key === "Enter" && saveBudget()}
                      placeholder="None"
                      aria-label="Budget in USD"
                    />
                  </InputGroup>
                </div>
                {budgetDirty && (
                  <Button
                    size="sm"
                    stretch="auto"
                    variant="secondary"
                    label="Save"
                    disabled={busy}
                    onClick={saveBudget}
                  />
                )}
              </SettingRow>
              <li className="px-4 py-3.5">
                <BudgetMeter spent={spent} budget={project.budget_usd} />
              </li>
            </Rows>
          </Section>

          <Section title="Danger zone">
            <Rows as="ul">
              <SettingRow
                label="Transfer ownership"
                hint="Hand this project to someone else - you stay on as an editor."
              >
                <TransferOwnership projectId={project.id} />
              </SettingRow>
            </Rows>
          </Section>
        </>
      )}
    </div>
  );
}

/** The month so far against the ceiling - a quiet bar, red only once it's blown. */
function BudgetMeter({
  spent,
  budget,
}: {
  spent: string | null;
  budget: string | null | undefined;
}) {
  const s = Number(spent ?? "0");
  if (!budget) {
    return (
      <p className="text-muted-foreground text-caption-md-regular">
        <Money usd={spent ?? "0"} className="text-foreground" /> spent this month · no ceiling
      </p>
    );
  }
  const b = Number(budget);
  const pct = Math.min(100, Math.max(0, (s / b) * 100 || 0));
  return (
    <div className="space-y-1.5">
      <div className="bg-muted h-1.5 overflow-hidden rounded-full">
        <div
          className={s >= b ? "bg-destructive h-full" : "bg-primary h-full"}
          style={{ width: `${pct}%` }}
        />
      </div>
      <p className="text-muted-foreground text-caption-md-regular">
        <Money usd={spent ?? "0"} className="text-foreground" /> of <Money usd={budget} /> this
        month
      </p>
    </div>
  );
}

// ── Context: the project's standing voice - the charter and the shared ledger ──────────────────

/** Two lanes with two different voices, and the roles differ on purpose: instructions are the
 *  OWNER's charter over every task filed here; the memory ledger is the EDITORS' shared notes.
 *  Both are visible to every member - nothing a project tells its tasks is hidden from its people. */
function ContextSection({
  project,
  members,
  isOwner,
}: {
  project: Route.ComponentProps["loaderData"]["project"];
  members: MemberRow[];
  isOwner: boolean;
}) {
  // ProjectOut.role honestly reports owner-or-viewer only (a per-row role needs the members) -
  // so the editor test is made here, against the membership the loader already fetched.
  const myId = useViewer()?.id;
  const canEdit = isOwner || members.some((m) => m.actor_id === myId && m.role === "editor");

  return (
    <>
      <Section title="Instructions">
        <Rows as="div">
          <div className="space-y-2 px-4 py-3.5">
            <p className="text-muted-foreground text-caption-md-regular">
              The project's charter - applied to every task filed here. Set by the owner; visible to
              everyone in the project. Tasks see it as their “Project instructions” context.
            </p>
            {/* Keyed: sidebar navigation swaps params on this mounted route, and both hold
                per-project state (the charter draft, the composer) that must not travel. */}
            <InstructionsEditor key={project.id} project={project} isOwner={isOwner} />
          </div>
        </Rows>
      </Section>

      <Section title="Project memory">
        <ProjectLedger
          key={project.id}
          projectId={project.id}
          members={members}
          myId={myId}
          canEdit={canEdit}
        />
      </Section>
    </>
  );
}

function InstructionsEditor({
  project,
  isOwner,
}: {
  project: Route.ComponentProps["loaderData"]["project"];
  isOwner: boolean;
}) {
  // The PATCH response is the canonical charter - synced locally, no revalidate: refiring
  // the project+members+usage loaders to learn one string we already hold was three refetches.
  const [saved, setSaved] = useState(project.instructions);
  const [text, setText] = useState(project.instructions);
  const [busy, setBusy] = useState(false);
  const dirty = text.trim() !== saved;

  async function save() {
    setBusy(true);
    try {
      const updated = await unwrap(
        api.PATCH("/v1/projects/{project_id}", {
          params: { path: { project_id: project.id } },
          body: {
            instructions: text.trim(),
            clear_budget: false,
            clear_icon: false,
            clear_hue: false,
          },
        }),
      );
      setText(updated.instructions);
      setSaved(updated.instructions);
      toast.success("Saved.");
    } catch (e) {
      toastError(e, "Couldn't save the instructions.");
    } finally {
      setBusy(false);
    }
  }

  if (!isOwner) {
    return project.instructions ? (
      <p className="text-body-sm-regular whitespace-pre-wrap">{project.instructions}</p>
    ) : (
      <p className="text-muted-foreground text-body-sm-regular">No standing instructions yet.</p>
    );
  }
  return (
    <>
      <TextAreaField
        size="lg"
        autoResize
        value={text}
        onChange={(e) => setText(e.currentTarget.value)}
        placeholder={"Cite sources.\nUK English.\nNumbers as tables, not prose."}
        maxLength={4000}
        rows={4}
        aria-label="Project instructions"
      />
      {dirty && (
        <Button
          size="sm"
          stretch="auto"
          variant="secondary"
          label="Save"
          disabled={busy}
          onClick={() => void save()}
        />
      )}
    </>
  );
}

function ProjectLedger({
  projectId,
  members,
  myId,
  canEdit,
}: {
  projectId: string;
  members: MemberRow[];
  myId: string | undefined;
  canEdit: boolean;
}) {
  // The project id is part of the query's identity: navigating A → B resets and refetches, so
  // B can never paint A's ledger (and a stale in-flight response is dropped by the hook).
  const { items, loadMore, loadingMore, hasMore, mutate } = useKeysetList<MemoryRow>({
    fetchPage: (cursor) =>
      unwrap(
        api.GET("/v1/memories", {
          params: {
            query: {
              scope: "project",
              project_id: projectId,
              limit: PAGE,
              ...(cursor ? { cursor } : {}),
            },
          },
        }),
      ),
    deps: [projectId],
    orderKey: (m) => m.created_at,
  });

  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);

  async function add() {
    if (!text.trim()) return;
    setBusy(true);
    try {
      const created = await unwrap(
        api.POST("/v1/memories", {
          body: {
            scope: "project",
            kind: "note",
            text: text.trim(),
            project_id: projectId,
          },
        }),
      );
      setText("");
      mutate((cur) => [created, ...cur]);
    } catch (e) {
      toastError(e, "Couldn't add that.");
    } finally {
      setBusy(false);
    }
  }

  const onEdited = useCallback(
    (next: MemoryRow) => mutate((cur) => cur.map((m) => (m.id === next.id ? next : m))),
    [mutate],
  );
  const onRetired = useCallback(
    (id: string) => mutate((cur) => cur.filter((m) => m.id !== id)),
    [mutate],
  );

  // A Map, not a per-row linear scan - and a stable meta callback so the memoized rows hold.
  const authors = useMemo(() => new Map(members.map((m) => [m.actor_id, m.name])), [members]);
  const meta = useCallback(
    (m: MemoryRow) => (
      <>
        added by {m.created_by_id === myId ? "you" : authors.get(m.created_by_id) || "the owner"} ·{" "}
        {ago(m.created_at)}
        {m.use_count > 0 && ` · used ${m.use_count} ${m.use_count === 1 ? "time" : "times"}`}
      </>
    ),
    [authors, myId],
  );

  return (
    <div className="space-y-2">
      <p className="text-muted-foreground px-4 text-caption-md-regular">
        Durable notes for every task in this project - shared with everyone here, added and edited
        by editors. Each note says who wrote it; tasks see them as their “Project memory” context.
      </p>
      {items === null ? (
        <LoadingState />
      ) : items.length === 0 && !canEdit ? (
        <EmptyState
          compact
          icon={Brain}
          title="Nothing remembered here yet"
          description="Editors can add durable notes that shape every task in this project."
        />
      ) : (
        <>
          <Rows as="ul">
            {items.map((m) => (
              <MemoryRowItem
                key={m.id}
                memory={m}
                canMutate={canEdit}
                onEdited={onEdited}
                onRetired={onRetired}
                meta={meta}
              />
            ))}
            {canEdit && (
              <li className="flex items-center gap-2 px-4 py-3">
                <div className="min-w-0 flex-1">
                  <InputGroup size="md">
                    <Input
                      size="md"
                      value={text}
                      onChange={(e) => setText(e.currentTarget.value)}
                      onKeyDown={(e) => e.key === "Enter" && void add()}
                      placeholder="Remember something for this project…"
                      maxLength={500}
                      aria-label="Add a project memory"
                    />
                  </InputGroup>
                </div>
                <Button
                  size="sm"
                  stretch="auto"
                  variant="secondary"
                  icon={<Icon icon={Plus} />}
                  label="Add"
                  disabled={busy || !text.trim()}
                  onClick={() => void add()}
                />
              </li>
            )}
          </Rows>
          <LoadMore hasMore={hasMore} busy={loadingMore} onClick={() => void loadMore()} />
        </>
      )}
    </div>
  );
}

function InviteMember({ projectId }: { projectId: string }) {
  const [open, setOpen] = useState(false);
  const [email, setEmail] = useState("");
  const [role, setRoleValue] = useState("editor");
  const [busy, setBusy] = useState(false);
  const revalidator = useRevalidator();

  async function submit() {
    if (!email.trim()) return;
    setBusy(true);
    try {
      await unwrap(
        api.POST("/v1/projects/{project_id}/members", {
          params: { path: { project_id: projectId } },
          body: { email: email.trim(), role },
        }),
      );
      setOpen(false);
      setEmail("");
      revalidator.revalidate();
    } catch (e) {
      toastError(e, "Couldn't add that member.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger
        render={
          <Button
            variant="secondary"
            size="sm"
            stretch="auto"
            icon={<Icon icon={UserPlus} />}
            label="Invite"
          />
        }
      />
      <DialogContent size="xs">
        <DialogMain>
          <DialogHeader>
            <DialogHeading>
              <DialogTitle>Invite to this project</DialogTitle>
              <DialogDescription>
                They'll see its tasks and artifacts. Editors can create, run, and resolve; viewers
                read.
              </DialogDescription>
            </DialogHeading>
          </DialogHeader>
          <DialogBody>
            <div className="space-y-3">
              <InputField
                size="lg"
                orientation="vertical"
                label="Email"
                type="email"
                value={email}
                onChange={(e) => setEmail(e.currentTarget.value)}
                placeholder="teammate@example.com"
                autoFocus
              />
              <SelectField
                size="lg"
                variant="secondary"
                width="auto"
                label="Role"
                modal={false}
                value={role}
                onValueChange={(v) => v && setRoleValue(v)}
                options={ROLE_OPTIONS}
              />
            </div>
          </DialogBody>
        </DialogMain>
        <DialogActions>
          <Button
            variant="primary"
            size="md"
            stretch="auto"
            label="Send invite"
            onClick={() => void submit()}
            disabled={busy || !email.trim()}
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

function TransferOwnership({ projectId }: { projectId: string }) {
  const [open, setOpen] = useState(false);
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const revalidator = useRevalidator();

  async function submit() {
    if (!email.trim()) return;
    setBusy(true);
    try {
      await unwrap(
        api.POST("/v1/projects/{project_id}/transfer", {
          params: { path: { project_id: projectId } },
          body: { email: email.trim() },
        }),
      );
      setOpen(false);
      revalidator.revalidate();
      toast.success("Ownership transferred.");
    } catch (e) {
      toastError(e, "Couldn't transfer ownership.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger
        render={<Button variant="secondary" size="sm" stretch="auto" label="Transfer ownership" />}
      />
      <DialogContent size="xs">
        <DialogMain>
          <DialogHeader>
            <DialogHeading>
              <DialogTitle>Transfer ownership</DialogTitle>
              <DialogDescription>
                The new owner takes full control (members, budget, settings). You become an editor.
                This can't be undone from here.
              </DialogDescription>
            </DialogHeading>
          </DialogHeader>
          <DialogBody>
            <InputField
              size="lg"
              orientation="vertical"
              label="New owner's email"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.currentTarget.value)}
              placeholder="teammate@example.com"
              autoFocus
            />
          </DialogBody>
        </DialogMain>
        <DialogActions>
          <Button
            variant="danger"
            size="md"
            stretch="auto"
            label="Transfer ownership"
            onClick={() => void submit()}
            disabled={busy || !email.trim()}
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

export { RouteErrorBoundary as ErrorBoundary } from "~/components/layout/route-error";
