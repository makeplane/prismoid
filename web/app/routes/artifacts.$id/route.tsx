/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { ArtifactView } from "~/components/artifact/artifact-view";
import { rendererFor } from "~/components/artifact/renderers/registry";
import { api, unwrap } from "~/lib/api/client";
import { taskHref } from "~/lib/links";
import type { Route } from "./+types/route";

export function meta() {
  return [{ title: "Artifact · Prismoid" }];
}

/** Best-effort crumb data and the write bit: ArtifactView owns everything the page renders
 * (including revision switching); this fetch names the way BACK - the owning task - so the
 * breadcrumb is `Home › {task} › {artifact}` instead of a path segment with no route behind
 * it, and reads whether the reader may write here (`can_edit`: a Project viewer
 * reads only, and the server would refuse their save). */
export async function clientLoader({ params }: Route.ClientLoaderArgs) {
  try {
    const artifact = await unwrap(
      api.GET("/v1/artifacts/{artifact_id}", {
        params: { path: { artifact_id: params.id } },
      }),
    );
    let task: {
      id: string;
      title: string;
      project_id: string | null;
      can_edit: boolean;
    } | null = null;
    try {
      const t = await unwrap(
        api.GET("/v1/tasks/{task_id}", {
          params: { path: { task_id: artifact.task_id } },
        }),
      );
      task = {
        id: t.id,
        title: t.title ?? "",
        project_id: t.project_id,
        can_edit: t.can_edit,
      };
    } catch {
      // A viewer may open an artifact whose task it cannot read - the crumb degrades honestly,
      // and the page reads only (unsure is the safe direction).
    }
    return {
      artifact: { title: artifact.title, render: artifact.render },
      task,
    };
  } catch {
    return null; // the page still renders; ArtifactView shows its own error state
  }
}

export const handle = {
  trail: (data: unknown) => {
    const d = data as Awaited<ReturnType<typeof clientLoader>>;
    if (!d) return null;
    const back = d.task ? [{ label: d.task.title.trim() || "Task", to: taskHref(d.task) }] : [];
    return [...back, { label: d.artifact.title.trim() || "Artifact" }];
  },
};

export default function ArtifactDetail({ params, loaderData }: Route.ComponentProps) {
  // Wide by default: boards and tables need room (the collection is the page). Prose keeps
  // its own reading measure - MarkdownView self-caps - so a memo never stretches to fill this.
  //
  // The scroll contract (one scroller per surface). This route lives OUTSIDE the shared
  // page container so it can own its chrome per kind: DOCUMENTS reproduce the container's
  // page chrome (px-8 py-8 + reading measure) and the page scrolls; APP-FRAME kinds
  // (registry frame:"app") are full-bleed application screens - exactly the viewport
  // remainder under the topbar (h-14 + border), so the page NEVER scrolls and the lens
  // inside is the only vertical scroller. min-h keeps a floor on short viewports (below it
  // the page scrolls again rather than crushing the lens).
  const app = rendererFor(loaderData?.artifact.render)?.frame === "app";
  const readOnly = !loaderData?.task?.can_edit;
  if (app) {
    return (
      <div className="flex h-[calc(100dvh-3.5rem-1px)] min-h-120 w-full flex-col px-6 pt-5 pb-5">
        <ArtifactView artifactId={params.id} readOnly={readOnly} />
      </div>
    );
  }
  return (
    <div className="w-full px-8 py-8">
      <div className="mx-auto w-full max-w-6xl">
        <ArtifactView artifactId={params.id} readOnly={readOnly} />
      </div>
    </div>
  );
}

export { RouteErrorBoundary as ErrorBoundary } from "~/components/layout/route-error";
