/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The bots messenger (the approved canvas): a two-pane surface in the Inbox's
// anatomy. Left - every bot as a conversation row (face · name · age · description line ·
// pin). Right - the selected bot, chat-first: Chat · Runs · Artifacts · Configure. The pin
// on each row is the sidebar law: a bot shows in the left nav only while pinned.

import { BotIcon, Pin, Plus, X } from "lucide-react";
import { useState } from "react";
import {
  Link,
  useNavigate,
  useParams,
  useRevalidator,
  useSearchParams,
  type ShouldRevalidateFunctionArgs,
} from "react-router";
import { toast } from "@prismoid/blocks/toast";
import { BotAvatar, IconPicker } from "~/components/icon-picker";
import { EmptyState, Pill, QuietDisclosure, SearchBox } from "~/components/kit";
import { TopbarPortal } from "~/components/layout/topbar-slot";
import { Button } from "@makeplane/propel/components/button";
import { Icon } from "@makeplane/propel/components/icon";
import { IconButton } from "@makeplane/propel/components/icon-button";
import { Checkbox } from "@makeplane/propel/components/checkbox";
import { InputField } from "@makeplane/propel/components/input-field";
import { TextAreaField } from "@makeplane/propel/components/text-area-field";
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
import { Popover, PopoverContent, PopoverTrigger } from "@makeplane/propel/components/popover";
import { Tab as TabItem, Tabs, TabsList } from "@makeplane/propel/components/tabs";
import { api, toastError, unwrap } from "~/lib/api/client";
import { ago } from "~/lib/format";
import { capitalize, cn } from "~/lib/utils";
import type { components } from "~/lib/api/types.gen";
import type { Route } from "./+types/route";
import { BotChat } from "./bot-chat";
import { ArtifactsTab, RunsTab } from "./shelves";
import { ConfigureTab } from "./configure-tab";
import { MemoryTab } from "./memory-tab";
import { RoutinesTab } from "./routines-tab";
import { AccessTab } from "./access-tab";
import { ChannelsTab } from "./channels-tab";

type BotRow = components["schemas"]["BotOut"];

export function meta({ params }: Route.MetaArgs) {
  return [
    {
      title: params.name ? `${params.name} · Bots · Prismoid` : "Bots · Prismoid",
    },
  ];
}

export async function clientLoader() {
  const page = await unwrap(api.GET("/v1/bots", { params: { query: { limit: 200 } } }));
  return { items: page.items };
}
clientLoader.hydrate = true as const;

// Picking another bot or another tab moves inside the list this page already holds: the router
// would otherwise hold the click until the list was fetched again. A change to the list asks
// for its own reload (the revalidator), which arrives here with the URL unchanged.
export function shouldRevalidate({
  currentUrl,
  nextUrl,
  formMethod,
  defaultShouldRevalidate,
}: ShouldRevalidateFunctionArgs) {
  if (formMethod) return defaultShouldRevalidate;
  const moved = currentUrl.pathname !== nextUrl.pathname || currentUrl.search !== nextUrl.search;
  return moved ? false : defaultShouldRevalidate;
}

// The trail is just "Bots": the pane header already names the selected bot, and a name crumb
// grows the breadcrumb straight into the pane strip's search box (seen live - they collided).
export const handle = {
  trail: () => [{ label: "Bots", to: "/bots" }],
};

export type BotHead = {
  head: BotRow;
  newest: BotRow;
  draftAhead: BotRow | null;
};

/** One row per name: the published head fronts it; a draft ahead is worth its own quiet note. */
export function heads(items: BotRow[]): BotHead[] {
  const byName = new Map<string, BotRow[]>();
  for (const b of items) {
    const list = byName.get(b.name) ?? [];
    list.push(b);
    byName.set(b.name, list);
  }
  return [...byName.entries()]
    .map(([, versions]) => {
      versions.sort((a, b) => b.version - a.version);
      const published = versions.find((v) => v.status === "published");
      const newest = versions[0];
      const head = published ?? newest;
      return {
        head,
        newest,
        draftAhead:
          published && newest.status === "draft" && newest.version > published.version
            ? newest
            : null,
      };
    })
    .sort((a, b) => a.head.name.localeCompare(b.head.name));
}

const STATUS_TONE = {
  published: "success",
  draft: "warning",
  archived: "muted",
} as const;
const EVAL_TONE = {
  green: "success",
  red: "danger",
  untested: "muted",
} as const;

export default function Bots({ loaderData }: Route.ComponentProps) {
  const { items } = loaderData;
  const selected = useParams().name ?? null;
  const revalidator = useRevalidator();
  const [q, setQ] = useState("");
  const needle = q.trim().toLowerCase();
  const all = heads(items);
  const rows = all.filter(
    (r) =>
      !needle || r.head.name.includes(needle) || r.head.description.toLowerCase().includes(needle),
  );
  // The open bot comes from every bot, not the search's rows: a search narrows the list and
  // never closes the pane (or remounts it, which threw away unsaved Configure edits).
  const current = selected ? all.find((r) => r.head.name === selected) : undefined;
  const pinnedCount = rows.filter((r) => r.head.pinned).length;

  return (
    // Full-bleed two-pane, the inbox's exact anatomy: this route lives OUTSIDE the page
    // container (routes.ts, beside /inbox), so no gutter or py-8 ever wraps it - the panes
    // run from the topbar to the window edge and the page itself never scrolls. The first
    // cut sat inside the container and floated in its padding; that placement was the bug,
    // not the styling.
    <div className="flex h-[calc(100svh-3.5rem)] min-w-0 flex-row">
      {/* The pane's standing controls ride the TOPBAR strip over the pane (the inbox's
          grammar): search + New bot right-aligned at the pane's edge, and the pane divider
          continues up through the strip - same line, not a second one beside it. */}
      <TopbarPortal side="start">
        <div aria-hidden className="absolute inset-y-0 border-l" style={{ left: PANE_W - 1 }} />
        <div
          className="absolute inset-y-0 left-0 flex items-center justify-end gap-1.5 pr-4"
          style={{ width: PANE_W }}
        >
          <SearchBox value={q} onChange={setQ} placeholder="Search bots…" className="w-44" />
          <CreateBot />
        </div>
      </TopbarPortal>

      <div className="flex shrink-0 flex-col border-r" style={{ width: PANE_W }}>
        <div className="scrollbar-thin px-gutter-pane min-h-0 flex-1 overflow-y-auto pt-4 pb-3">
          {rows.length === 0 ? (
            <EmptyState
              compact
              icon={BotIcon}
              title={needle ? `No matches for “${q.trim()}”` : "No bots yet"}
              description={
                needle ? undefined : "Create one: a face, a job, and the evals that keep it honest."
              }
              className="mt-2"
            />
          ) : (
            <ul className="space-y-0.5">
              {rows.map((r) => (
                <BotListRow
                  key={r.head.name}
                  row={r}
                  selected={r.head.name === selected}
                  onPinned={() => revalidator.revalidate()}
                />
              ))}
            </ul>
          )}
        </div>
        <div className="text-muted-foreground px-gutter-pane border-t py-2 text-caption-md-regular">
          {rows.length} bot{rows.length === 1 ? "" : "s"} · {pinnedCount} pinned to sidebar
        </div>
      </div>

      <div className="flex min-w-0 flex-1 flex-col">
        {current ? (
          <BotPane
            key={current.head.name}
            row={current}
            onChanged={() => revalidator.revalidate()}
          />
        ) : (
          // Centered in the FULL pane height - the inbox's empty-detail treatment.
          <div className="flex h-full items-center justify-center">
            <EmptyState
              icon={BotIcon}
              title="Pick a bot"
              description="Its conversation, runs, artifacts and configuration open here."
            />
          </div>
        )}
      </div>
    </div>
  );
}

function BotListRow({
  row,
  selected,
  onPinned,
}: {
  row: BotHead;
  selected: boolean;
  onPinned: () => void;
}) {
  const { head } = row;
  const [pinned, setPinned] = useState(head.pinned);
  const [busy, setBusy] = useState(false);

  async function togglePin(e: React.MouseEvent) {
    e.preventDefault(); // the row is a link; the pin is its own gesture
    e.stopPropagation();
    if (busy) return;
    setBusy(true);
    const next = !pinned;
    setPinned(next); // optimistic - the sidebar settles on revalidate
    try {
      await unwrap(
        api.PUT("/v1/bots/{name}/pin", {
          params: { path: { name: head.name } },
          body: { pinned: next },
        }),
      );
      onPinned();
    } catch (err) {
      setPinned(!next);
      toastError(err, "Couldn't change the pin.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <li>
      <Link
        to={`/bots/${head.name}`}
        aria-current={selected || undefined}
        className={cn(
          "hover:bg-accent/40 flex w-full items-start gap-2.5 rounded-md px-2.5 py-2 text-left transition-colors",
          selected && "bg-accent/60",
        )}
      >
        <BotAvatar
          name={head.name}
          icon={head.icon}
          hue={head.icon_hue}
          size="sm"
          className="mt-0.5"
        />
        <span className="min-w-0 flex-1">
          <span className="flex items-baseline justify-between gap-2">
            <span className="truncate text-body-sm-regular font-medium">{head.name}</span>
            <span className="text-muted-foreground shrink-0 text-caption-md-regular">
              {ago(head.created_at)}
            </span>
          </span>
          <span className="text-muted-foreground block truncate text-caption-md-regular">
            {head.description}
          </span>
        </span>
        <button
          type="button"
          onClick={(e) => void togglePin(e)}
          aria-label={
            pinned ? `Unpin ${head.name} from the sidebar` : `Pin ${head.name} to the sidebar`
          }
          title={pinned ? "Pinned · shown in the sidebar" : "Pin to show in the sidebar"}
          className={cn(
            "mt-1 shrink-0 transition-colors",
            pinned ? "text-foreground" : "text-muted-foreground/50 hover:text-muted-foreground",
          )}
        >
          <Pin className="size-3.5" fill={pinned ? "currentColor" : "none"} />
        </button>
      </Link>
    </li>
  );
}

const PANE_W = 340;

const TABS = [
  "chat",
  "runs",
  "artifacts",
  "memory",
  "routines",
  "channels",
  "access",
  "configure",
] as const;
type Tab = (typeof TABS)[number];

function BotPane({ row, onChanged }: { row: BotHead; onChanged: () => void }) {
  const { head, newest, draftAhead } = row;
  const working = draftAhead ?? head;
  const [searchParams, setSearchParams] = useSearchParams();
  const tab = (TABS as readonly string[]).includes(searchParams.get("tab") ?? "")
    ? (searchParams.get("tab") as Tab)
    : "chat";
  const setTab = (t: Tab) =>
    setSearchParams(
      (p) => {
        const n = new URLSearchParams(p);
        if (t === "chat") n.delete("tab");
        else n.set("tab", t);
        return n;
      },
      { replace: true },
    );

  return (
    <>
      {/* The identity block owns the top - the PageHeader grammar (mark · xl title · quiet
          subtitle), pills beside the name, and the LINE tab rail resting on the one hairline.
          The boxed TabsList next to two outlined pills read as boxes-on-boxes and looked crushed:
          the underline variant keeps the rail weightless. */}
      <div className="border-b px-6 pt-5">
        <div className="flex items-start gap-3">
          <BotFace name={head.name} icon={head.icon} hue={head.icon_hue} onChanged={onChanged} />
          <div className="min-w-0 flex-1 space-y-1">
            <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
              <h2 className="font-heading min-w-0 truncate text-h4-regular font-semibold tracking-tight">
                {head.name}
              </h2>
              <Pill tone={STATUS_TONE[working.status as keyof typeof STATUS_TONE] ?? "muted"}>
                <span className="capitalize">{working.status}</span>
                <span className="tabular-nums">&nbsp;v{working.version}</span>
              </Pill>
              <Pill tone={EVAL_TONE[working.eval_status as keyof typeof EVAL_TONE] ?? "muted"}>
                evals {working.eval_status}
              </Pill>
            </div>
            <p className="text-muted-foreground truncate text-body-sm-regular">
              {working.title || jobLine(working.description)}
            </p>
          </div>
        </div>
        <div className="mt-2 -ml-2.5">
          <Tabs variant="underline" value={tab} onValueChange={(v) => setTab(v as Tab)}>
            <TabsList>
              {TABS.map((t) => (
                <TabItem key={t} value={t} label={capitalize(t)} />
              ))}
            </TabsList>
          </Tabs>
        </div>
      </div>
      {tab === "chat" && <BotChat name={head.name} threadsMode={head.threads} />}
      {tab === "runs" && <RunsTab name={head.name} />}
      {tab === "artifacts" && <ArtifactsTab name={head.name} />}
      {tab === "memory" && <MemoryTab name={head.name} projectId={head.project_id} />}
      {tab === "routines" && <RoutinesTab name={head.name} projectId={head.project_id} />}
      {tab === "channels" && <ChannelsTab name={head.name} projectId={head.project_id} />}
      {tab === "access" && <AccessTab bot={draftAhead ?? head} />}
      {tab === "configure" && (
        <ConfigureTab head={head} newest={newest} draftAhead={draftAhead} onChanged={onChanged} />
      )}
    </>
  );
}

/** The subtitle is the JOB, not the doctrine: the description up to its routing clauses. */
function jobLine(description: string): string {
  return description.split(/\s*Load when\b/i)[0]?.trim() || description;
}

/** The face in the pane header - click to change (the ProjectFace pattern, optimistic). */
function BotFace({
  name,
  icon,
  hue,
  onChanged,
}: {
  name: string;
  icon: string | null;
  hue: number | null;
  onChanged: () => void;
}) {
  const [face, setFace] = useState({ icon, hue });

  async function pick(next: { icon?: string | null; hue?: number | null }) {
    const prev = face;
    setFace({
      icon: next.icon !== undefined ? next.icon : prev.icon,
      hue: next.hue !== undefined ? next.hue : prev.hue,
    });
    const body = {
      clear_icon: false,
      clear_hue: false,
      clear_title: false,
    } as {
      icon?: string;
      icon_hue?: number;
      clear_icon: boolean;
      clear_hue: boolean;
      clear_title: boolean;
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
      await unwrap(api.PATCH("/v1/bots/{name}", { params: { path: { name } }, body }));
      onChanged();
    } catch (e) {
      setFace(prev);
      toastError(e, "Couldn't change the face.");
    }
  }

  return (
    <Popover>
      <PopoverTrigger
        aria-label="Change this bot's face"
        render={
          <button
            type="button"
            className="hover:ring-foreground/25 rounded-md ring-1 ring-transparent transition-shadow"
          />
        }
      >
        <BotAvatar name={name} icon={face.icon} hue={face.hue} size="lg" />
      </PopoverTrigger>
      <PopoverContent align="start">
        <div className="w-72">
          <IconPicker icon={face.icon} hue={face.hue} onPick={(n) => void pick(n)} />
        </div>
      </PopoverContent>
    </Popover>
  );
}

/** Creation asks for ONE thing: a name (nothing
 *  else is mandatory at birth). Instructions sit on top, optional - leave them blank and the
 *  drafter writes the first version. Routing, evals, and everything else live under Advanced
 *  and at publish time, where the gate names whatever is still missing. */
function CreateBot() {
  const navigate = useNavigate();
  const revalidator = useRevalidator();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [instructions, setInstructions] = useState("");
  const [expand, setExpand] = useState(true);
  const [job, setJob] = useState("");
  const [useWhen, setUseWhen] = useState("");
  const [notFor, setNotFor] = useState("");
  const [firstGoal, setFirstGoal] = useState("");
  const [busy, setBusy] = useState(false);
  const phrases = (s: string) =>
    s
      .split(",")
      .map((p) => p.trim())
      .filter(Boolean);
  const ready = name.trim().length > 0;

  async function create() {
    if (!ready) return;
    setBusy(true);
    try {
      const hasRouting = job.trim() || phrases(useWhen).length > 0 || phrases(notFor).length > 0;
      const bot = await unwrap(
        api.POST("/v1/bots", {
          body: {
            name: name.trim(),
            ...(instructions.trim() ? { instructions: instructions.trim() } : {}),
            ...(hasRouting
              ? {
                  job: job.trim(),
                  use_when: phrases(useWhen),
                  not_for: phrases(notFor),
                }
              : {}),
            ...(firstGoal.trim()
              ? { cases: [{ goal: firstGoal.trim(), expect: name.trim() }] }
              : {}),
            can_ask_user: true,
          },
        }),
      );
      // The bot exists from here: the drafter is a second ask with its own outcome, never a
      // reason to say the bot was not created (a retry would 409 on the taken name).
      if (expand && !instructions.trim()) {
        try {
          await unwrap(
            api.POST("/v1/bots/{name}/draft", {
              params: { path: { name: bot.name } },
            }),
          );
          toast.success(`${bot.name} created - writing its first instructions now.`);
        } catch (e) {
          toastError(
            e,
            `${bot.name} was created, but the drafter couldn't start - try Draft for me on Configure.`,
          );
        }
      } else {
        toast.success(`${bot.name} created as a draft.`);
      }
      setOpen(false);
      // The list is not re-read on a move inside this page, so the new bot joins it first.
      await revalidator.revalidate();
      navigate(`/bots/${bot.name}?tab=configure`);
    } catch (e) {
      toastError(e, "Couldn't create the bot.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger
        render={
          <IconButton
            variant="primary"
            size="sm"
            icon={<Icon icon={Plus} />}
            aria-label="New bot"
          />
        }
      />
      <DialogContent size="xs">
        <DialogMain>
          <DialogHeader>
            <DialogHeading>
              <DialogTitle>New bot</DialogTitle>
              <DialogDescription>
                A name is all it needs. Teach it the rest as you go - publishing tells you what is
                still missing.
              </DialogDescription>
            </DialogHeading>
          </DialogHeader>
          <DialogBody>
            <div className="space-y-4">
              <InputField
                size="lg"
                orientation="vertical"
                label="Name"
                value={name}
                onChange={(e) => setName(e.currentTarget.value)}
                placeholder="contract-reviewer"
                autoFocus
              />
              <div className="space-y-1.5">
                <TextAreaField
                  size="lg"
                  autoResize
                  label="Instructions (optional)"
                  value={instructions}
                  onChange={(e) => setInstructions(e.currentTarget.value)}
                  placeholder="What this bot should know and do. Leave blank and the drafter writes the first version."
                  rows={4}
                />
                {!instructions.trim() && (
                  <Checkbox
                    label="Write instructions for me after creating"
                    checked={expand}
                    onCheckedChange={(v) => setExpand(v === true)}
                  />
                )}
              </div>
              <QuietDisclosure label="Advanced">
                <div className="space-y-3 pt-2">
                  <InputField
                    size="lg"
                    orientation="vertical"
                    label="What does this bot do?"
                    value={job}
                    onChange={(e) => setJob(e.currentTarget.value)}
                    placeholder="Reviews vendor contracts against our standard terms"
                  />
                  <InputField
                    size="lg"
                    orientation="vertical"
                    label="When should it step in?"
                    hint="Commas separate triggers. Needed before publishing, not now."
                    value={useWhen}
                    onChange={(e) => setUseWhen(e.currentTarget.value)}
                    placeholder="an MSA needs a close read, a vendor sends new terms"
                  />
                  <InputField
                    size="lg"
                    orientation="vertical"
                    label="What should it refuse?"
                    value={notFor}
                    onChange={(e) => setNotFor(e.currentTarget.value)}
                    placeholder="drafting contracts, legal advice"
                  />
                  <InputField
                    size="lg"
                    orientation="vertical"
                    label="A goal it should win"
                    hint="Seeds its first eval - publishing needs the evals green."
                    value={firstGoal}
                    onChange={(e) => setFirstGoal(e.currentTarget.value)}
                    placeholder="Review this vendor MSA against our standard terms"
                  />
                </div>
              </QuietDisclosure>
            </div>
          </DialogBody>
        </DialogMain>
        <DialogActions>
          <Button
            variant="primary"
            size="md"
            stretch="auto"
            label="Create bot"
            onClick={() => void create()}
            disabled={busy || !ready}
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
