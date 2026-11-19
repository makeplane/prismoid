/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useEffect, useRef, useState, type ReactNode } from "react";
import { useLocation } from "react-router";
import type { ComposerApi } from "~/components/composer/composer";
import { TaskComposer, loadComposerOptions } from "~/components/composer/task-composer";
import type { ProjectRef } from "~/components/composer/project-picker";
import { PageLoading, UserBubble } from "~/components/kit";
import { api, unwrap } from "~/lib/api/client";
import { cn } from "~/lib/utils";
import { STORAGE } from "~/lib/storage";
import { RecentTasks } from "./recent-tasks";
import type { Route } from "./+types/route";

export function meta() {
  return [{ title: "Prismoid" }];
}

const LAST_PROJECT_KEY = STORAGE.lastProject;

// The hello rotates - human, time-aware. The mechanics live in the composer's placeholder;
// the heading is just a greeting that knows what time it is.
const MORNING = ["Morning.", "Early bird.", "Fresh start.", "Coffee's on."];
const AFTERNOON = ["Back at it.", "Good afternoon.", "Keep it rolling."];
const EVENING = ["Good evening.", "Golden hour.", "One more for today?"];
const NIGHT = ["Night owl.", "The quiet hours.", "Late shift."];
const ANYTIME = ["What are we making?", "Ready when you are.", "Something on your mind?"];

function greeting(): string {
  const h = new Date().getHours();
  const timed = h < 5 ? NIGHT : h < 12 ? MORNING : h < 18 ? AFTERNOON : h < 23 ? EVENING : NIGHT;
  const pool = [...timed, ...ANYTIME];
  return pool[Math.floor(Math.random() * pool.length)];
}

export async function clientLoader() {
  // Destination memory: the last project used - the default does the filing. (A Project's own page
  // starts its tasks itself, scoped to it; the landing reads no `project` param.) Validated
  // against the API so a deleted project resets to none, never a 4xx submit.
  let candidate: string | null;
  try {
    const stored = JSON.parse(
      localStorage.getItem(LAST_PROJECT_KEY) ?? "null",
    ) as ProjectRef | null;
    candidate = stored?.id ?? null;
  } catch {
    candidate = null;
  }
  const [options, initialProject] = await Promise.all([
    loadComposerOptions(),
    candidate
      ? unwrap(
          api.GET("/v1/projects/{project_id}", {
            params: { path: { project_id: candidate } },
          }),
        )
          .then((s): ProjectRef | null => ({ id: s.id, name: s.name }))
          .catch(() => null)
      : Promise.resolve<ProjectRef | null>(null),
  ]);
  if (candidate && !initialProject) {
    localStorage.removeItem(LAST_PROJECT_KEY); // the remembered project is gone - forget it
  }
  return { options, initialProject };
}
clientLoader.hydrate = true as const;

export default function NewTask({ loaderData }: Route.ComponentProps) {
  const { options, initialProject } = loaderData;
  const [hello] = useState(greeting);
  // autoFocus covers the first arrival; New task (the pen, ⌘⇧O) landing on a Home already open
  // keeps this page mounted, so each new navigation puts the cursor back in the box.
  const composer = useRef<ComposerApi | null>(null);
  const { key } = useLocation();
  useEffect(() => composer.current?.focus(), [key]);
  // Enter answers at once: the ask and a loader take the page while the task is created and
  // its page loads. The composer stays mounted underneath (hidden), so a failed create brings
  // it back with the draft and its files intact.
  const [starting, setStarting] = useState<string | null>(null);
  // The box was hidden while the create was out, which drops focus: a failed create hands the
  // cursor back with the draft.
  const wasStarting = useRef(false);
  useEffect(() => {
    if (wasStarting.current && starting === null) composer.current?.focus();
    wasStarting.current = starting !== null;
  }, [starting]);

  return (
    <>
      {starting !== null && (
        <div className="mx-auto flex min-h-[calc(100svh-16rem)] w-full max-w-3xl flex-col gap-6 pt-6">
          <UserBubble className="line-clamp-6">{starting}</UserBubble>
          <PageLoading label="Starting your task…" />
        </div>
      )}
      <NewTaskColumn hidden={starting !== null}>
        {/* The greeting is not page chrome: the landing's hero sits sizes above the title recipe
            on purpose - it is the only heading on a page that IS the composer, worn large so
            coming back feels like an invitation, not a form (Inter's opsz axis serves the
            display cut up here). The how-to sentence lives in the composer's placeholder. */}
        <h1 className="font-heading text-center text-display font-semibold tracking-tight text-balance">
          {hello}
        </h1>

        <TaskComposer
          options={options}
          initialProject={initialProject}
          className="w-full"
          tall
          autoFocus
          apiRef={composer}
          onStarting={setStarting}
        />

        <RecentTasks />
      </NewTaskColumn>
    </>
  );
}

/** The landing zone: one hero line, one box that holds everything - goal, capture, and every
 *  setting on the box's own bottom row, the way modern ask bars work. ONE measure for the
 *  whole page: greeting, composer, and recents share a single centered column, so nothing
 *  sits on its own axis. */
function NewTaskColumn({ hidden, children }: { hidden: boolean; children: ReactNode }) {
  return (
    <div
      hidden={hidden}
      className={cn(
        "mx-auto min-h-[calc(100svh-16rem)] w-full max-w-3xl flex-col justify-center gap-8",
        hidden ? "hidden" : "flex",
      )}
    >
      {children}
    </div>
  );
}
