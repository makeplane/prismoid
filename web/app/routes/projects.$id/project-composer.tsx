/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The Project page's own task composer: a task starts HERE, scoped to
// this Project, and lands on /projects/<sid>/tasks/<tid>. It replaces the old "New task" link
// through the landing's `?project=` preselect - starting where the work lives needs no detour.
//
// It fetches its own dial rosters, so the Project page paints first, and it is mounted only for a
// caller who can start a task: a viewer fetches nothing, and an unrelated revalidation of the
// page (a rename, a member change) never refetches them.

import { useEffect, useState } from "react";
import {
  loadComposerOptions,
  TaskComposer,
  type ComposerOptions,
} from "~/components/composer/task-composer";
import { Skeleton, SkeletonItem } from "@makeplane/propel/components/skeleton";

/** Creating a task in a Project is an editor's verb: a viewer - who joined by
 *  link to read - is offered no write the server would refuse (c248b3b hid the old button the
 *  same way). */
export const canStartTask = (role: string): boolean => role !== "viewer";

/** No rosters is a composer without dials, never a missing composer. */
const NO_OPTIONS: ComposerOptions = {
  skills: [],
  fleetModels: [],
  connectors: [],
  catalog: null,
  bots: [],
};

type ProjectFacts = { id: string; name: string; role: string };

export function ProjectComposer({
  project,
  options,
}: {
  project: ProjectFacts;
  /** Hand the rosters in (a test, a host that has them); otherwise they are fetched once on mount. */
  options?: ComposerOptions;
}) {
  if (!canStartTask(project.role)) return null;
  // Keyed so a sidebar hop to another Project never carries the last one's draft or dials.
  return <ComposerForProject key={project.id} project={project} options={options} />;
}

function ComposerForProject({
  project,
  options,
}: {
  project: ProjectFacts;
  options?: ComposerOptions;
}) {
  const [fetched, setFetched] = useState<ComposerOptions | null>(null);
  useEffect(() => {
    if (options) return;
    let alive = true;
    loadComposerOptions().then(
      (o) => alive && setFetched(o),
      () => alive && setFetched(NO_OPTIONS),
    );
    return () => {
      alive = false;
    };
  }, [options]);

  const ready = options ?? fetched;
  // The box's resting height, held while the rosters load, so nothing below it jumps.
  if (!ready)
    return (
      <Skeleton aria-label="Loading the composer" aria-hidden="true" blockSize="5.75rem">
        <SkeletonItem />
      </Skeleton>
    );
  return (
    <TaskComposer
      options={ready}
      fixedProject={{ id: project.id, name: project.name }}
      className="w-full"
      placeholder={`Describe what you need done in ${project.name}. Attach anything that helps.`}
    />
  );
}
