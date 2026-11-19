/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// THE artifact renderer registry (ADR-015). Dispatch keys on the server-derived `artifact.render`
// hint - the registry in backend `schemas/outputs.py` answers "how do I present these bytes?"
// once, on the wire; this file only maps its answers to components. The old client-side
// `PREVIEWABLE` type list drifted into piping `.docx` zip bytes through <Markdown>.
//
// Adding a kind's renderer (the teammate story):
//   1. create `./<kind>-view.tsx` exporting a component of `RendererProps`
//   2. add ONE entry below - `source` says what the shell fetches before mounting
//      ("text" → the artifact bytes as a string on `props.text`; "none" → nothing)
//   3. heavy renderers use `Component: lazy(() => import("./<kind>/<kind>-view"))` so read mode
//      stays off the critical path (the shell already provides the boundary + fallback floor)
// The shell (`artifact-view.tsx`) owns fetching, loading states, the error boundary, and the
// DownloadCard floor - a renderer owns pixels only.
//
// A hint with NO entry (deliberately: "none") falls to the DownloadCard - the honest floor.
import { lazy, type ComponentType } from "react";
import type { components } from "~/lib/api/types.gen";
import { MarkdownView } from "./markdown-view";
import { TextView } from "./text-view";

export type ArtifactOut = components["schemas"]["ArtifactOut"];
export type DownloadOut = components["schemas"]["DownloadOut"];

export type RendererProps = {
  artifact: ArtifactOut;
  download: DownloadOut;
  text?: string; // present when the entry's `source` is "text"
  /** A renderer that minted a new revision calls this with the new artifact id;
   * the shell swaps the viewer onto it. Optional - most renderers never write. */
  onArtifactChanged?: (artifactId: string) => void;
  /** True when the host runs this renderer as an APP FRAME (entry.frame === "app"): the
   * renderer's root should fill its box (h-full min-h-0) and own THE one scroller. */
  fill?: boolean;
};

export type RendererEntry = {
  source: "text" | "none";
  /** The scroll contract. "app": this kind is an application surface - hosts hand it the
   * whole frame and kill their own scroll, so the ONLY scroller is inside the renderer
   * (documents scroll as pages; apps fill the frame). Omitted = document: normal flow,
   * the page/pane scroller owns reading. */
  frame?: "app";
  Component: ComponentType<RendererProps>;
};

export const RENDERERS: Record<string, RendererEntry> = {
  markdown: { source: "text", Component: MarkdownView },
  text: { source: "text", Component: TextView },
  // The first lazy renderer: six layouts of real code that the task page's critical path never
  // pays for. The shell's Suspense + boundary make a stale post-deploy chunk degrade to the
  // DownloadCard, never a white drawer.
  //
  // NOT an app frame (reverted same day it landed): a board bounded to the
  // frame scrolls INSIDE itself - a scroll-in-a-scroll the user named ugly twice. A
  // collection reads like a document now: full natural height, the page/pane owns the one
  // vertical scroll, the ViewShell pans wide layouts horizontally. `frame: "app"` remains
  // the contract for genuinely app-like kinds (a webapp preview); none ship today.
  collection: {
    source: "text",
    Component: lazy(() => import("./collection/collection-view")),
  },
  // Records as a table: .jsonl parses per line, .csv rides the ```csv block's
  // parser. The raw-text pre this replaces was the scatter map's wall of JSON.
  dataset: {
    source: "text",
    Component: lazy(() => import("./dataset-view")),
  },
  // The bytes ARE the preview: a .pdf rides the ONE inline lane - the
  // /rendition endpoint presigns it `inline` with its real content type (an attachment
  // disposition renders as a blank embed), and the browser's own viewer does the rest.
  pdf: {
    source: "none",
    Component: lazy(() => import("./rendition-view")),
  },
  // A coding task's deliverable on the device: it reads its own archive from the download link.
  change: {
    source: "none",
    Component: lazy(() => import("./change-view")),
  },
};

export function rendererFor(render: string | null | undefined): RendererEntry | undefined {
  return RENDERERS[render ?? ""];
}
