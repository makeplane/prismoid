/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { ArrowLeft, Files, ListTodo } from "lucide-react";
import type { ReactNode } from "react";
import { Link } from "react-router";
import { Icon } from "@makeplane/propel/components/icon";
import { IconButton } from "@makeplane/propel/components/icon-button";
import { FORMAT_LABEL, type DocumentFormat } from "~/components/composer/dials";
import { artifactKindLabel, Money, StatusMark, statusSpins, taskMark } from "~/components/kit";
import { TopbarPortal, TopbarYield } from "~/components/layout/topbar-slot";
import type { TaskStatus } from "~/lib/api/events";
import type { components } from "~/lib/api/types.gen";
import { projectHref } from "~/lib/links";
import type { PresentPerson } from "~/lib/presence";
import { callsLabel } from "~/lib/usage-ledger";
import { PresenceBar } from "./presence-bar";
import { SharePopover } from "./share-popover";
import { TaskMenu } from "./task-menu";

type TaskDetailOut = components["schemas"]["TaskDetailOut"];

// The path chip's tooltip per deliverable_source - whose call the kind was.
const DELIVERABLE_SOURCE_TITLE: Record<string, string> = {
  user: "The deliverable · set by your dial",
  auto: "The deliverable · detected from your ask",
  plan: "The deliverable · the planner's choice",
};

const CHIP = "text-muted-foreground text-caption-md-regular hidden md:inline";
const LINK_CHIP =
  "text-muted-foreground hover:text-foreground text-caption-md-regular transition-colors";

/** What the task resolved into and where it came from: quiet chips beside the spend. */
function TaskChips({ detail }: { detail: TaskDetailOut }) {
  const chatBot = detail.bot_pins?.[0]?.name ?? detail.direct_bot;
  return (
    <>
      {/* The resolved size: even an auto task shows which of the five it became. The marker is
          SERVER-composed ("extended · ~15 pages", unit included), so this chip can never
          disagree with plan titles or the CLI. */}
      {detail.size_marker && (
        <span
          className={CHIP}
          title="The task's size bucket - resolved from your dial or the goal's own numbers"
        >
          {detail.size_marker}
        </span>
      )}
      {/* The resolved PATH: which kind the thread is making, and whose call it was. Label
          composes client-side (kit/artifact-icon, the one vocabulary home) - unlike size there
          is no unit math for the server to own. Fleet never stamps, so the chip is absent
          there for free. */}
      {detail.deliverable && (
        <span
          className={CHIP}
          title={
            DELIVERABLE_SOURCE_TITLE[detail.deliverable_source ?? "user"] ??
            DELIVERABLE_SOURCE_TITLE.user
          }
        >
          {artifactKindLabel(detail.deliverable)}
          {/* The wire field is a plain string - an unknown value shows itself, never "undefined". */}
          {detail.deliverable_format
            ? ` · ${FORMAT_LABEL[detail.deliverable_format as DocumentFormat] ?? detail.deliverable_format}`
            : ""}
        </span>
      )}
      {/* Chat provenance: this task was started from a conversation - the result went home
          there too. Quiet, like the direct chip; links the bot's office. */}
      {detail.origin_conversation_id && (
        <Link
          to={`/bots/${chatBot ?? ""}`}
          className={LINK_CHIP}
          title="Started from a conversation - its delivery was reported back there"
        >
          From a chat with {chatBot ?? "a bot"}
        </Link>
      )}
      {/* The DM lane's keeper: one bot answers this whole thread. A quiet chip, linking its
          office. */}
      {detail.direct_bot && (
        <Link
          to={`/bots/${detail.direct_bot}`}
          className={LINK_CHIP}
          title="A direct thread - only this bot answers here"
        >
          Direct · {detail.direct_bot}
        </Link>
      )}
    </>
  );
}

/**
 * The task's chrome - ONE cluster, two homes: the shell topbar normally, the chat side's own
 * header band in artifact mode. Same elements, so no control ever vanishes; fork and every
 * task option stay one click away while reading.
 */
export function TaskChrome({
  detail,
  status,
  ending,
  spendUsd,
  calls,
  searches,
  people,
  artifactMode,
  railPressed,
  onShowProgress,
  onToggleRail,
  onFork,
  onControl,
}: {
  detail: TaskDetailOut;
  status: TaskStatus;
  ending?: string | null;
  spendUsd: string;
  calls: number;
  searches: number;
  people: readonly PresentPerson[];
  artifactMode: boolean;
  railPressed: boolean;
  onShowProgress: () => void;
  onToggleRail: () => void;
  onFork?: () => void;
  onControl?: (intent: "pause" | "resume" | "cancel") => void;
}) {
  return (
    <>
      {/* While the task is alive the mark disappears - three concurrent spinners was the
          named disease; the tail's fade trio is the ONE liveness home, and the ticking spend
          counter beside this is passive proof of life. The mark returns for every
          settled/parked state, where it carries real vocabulary. */}
      {!statusSpins(status) && <StatusMark status={taskMark(status, ending)} labelled />}
      <span className="text-muted-foreground text-caption-md-regular">
        <Money usd={spendUsd} className="text-foreground" />
        <span className="hidden md:inline"> · {callsLabel(calls, searches)}</span>
      </span>
      <TaskChips detail={detail} />
      {artifactMode && (
        <IconButton
          size="sm"
          variant="ghost"
          icon={<Icon icon={ListTodo} />}
          onClick={onShowProgress}
          aria-label="Progress and usage"
        />
      )}
      {/* Presence: who else has the task open, then Share. Both sit BEFORE the view controls -
          people and access first, then the rail toggle, then ⋯. */}
      <PresenceBar people={people} />
      <SharePopover detail={detail} />
      {/* the rail toggle - Files, not a generic panel glyph: the button names what the
          window holds. In artifact mode it reads "give me the rail back" (closes the
          pane). Order: info (dot · spend · calls), then view controls, then ⋯ - overflow
          closes the row, the convention. */}
      <IconButton
        size="sm"
        variant="ghost"
        icon={<Icon icon={Files} />}
        onClick={onToggleRail}
        aria-pressed={railPressed}
        aria-label="Toggle plan and artifacts"
      />
      <TaskMenu
        access={{
          canEdit: detail.can_edit,
          canCurate: detail.can_curate,
          isCreator: detail.is_creator,
        }}
        // Fork mints a new task that carries the whole thread - a write, like the ones below.
        onFork={onFork}
        taskId={detail.id}
        title={detail.title}
        goal={detail.goal}
        status={status}
        archivedAt={detail.archived_at}
        // Pause/resume/cancel are editor verbs: a viewer's menu simply doesn't offer them.
        onControl={onControl}
      />
    </>
  );
}

/** Where the chrome lives: the shell TOPBAR normally - the chat side's own h-14 header band
 *  (back link, title, the same chrome) when the artifact takes its plane and the shared topbar
 *  yields. */
export function TaskHeader({
  artifactMode,
  detail,
  title,
  chrome,
}: {
  artifactMode: boolean;
  detail: Pick<TaskDetailOut, "project_id" | "title" | "goal">;
  /** The stream's minted title, when one has landed. */
  title?: string | null;
  chrome: ReactNode;
}) {
  if (!artifactMode) return <TopbarPortal>{chrome}</TopbarPortal>;
  const projectId = detail.project_id;
  return (
    <>
      <TopbarYield />
      <div className="flex h-14 shrink-0 items-center gap-2 border-b px-3">
        {/* An icon-only link: AnchorButton needs a label, so IconButton renders the Link. */}
        <IconButton
          nativeButton={false}
          variant="ghost"
          size="sm"
          icon={<Icon icon={ArrowLeft} />}
          aria-label={projectId ? "Back to the Project" : "Back to tasks"}
          render={<Link to={projectId ? projectHref(projectId) : "/tasks"} />}
        />
        <h1 className="min-w-0 flex-1 truncate text-body-sm-regular font-medium">
          {title ?? detail.title ?? detail.goal}
        </h1>
        {chrome}
      </div>
    </>
  );
}
