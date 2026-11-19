/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { Download } from "lucide-react";
import { IconTile, MetaChip, artifactIcon, artifactKindLabel } from "~/components/kit";
import { Button } from "@makeplane/propel/components/button";
import { Icon } from "@makeplane/propel/components/icon";
import { formatBytes } from "~/lib/format";
import type { ArtifactOut, DownloadOut } from "./renderers/registry";

// One honest sentence per kind - never a silent absence, never mojibake. Since
// a preview usually renders ABOVE this card, so the sentence sells the download, not
// the absence.
const NO_PREVIEW: Record<string, string> = {
  spreadsheet: "Download to open the workbook in Excel or Google Sheets.",
  deck: "Download to open the deck in PowerPoint or Google Slides.",
  code: "A code artifact is its project archive - download and extract it.",
  webapp: "A web app artifact is its project archive - download and extract it.",
  change: "A change is its report, patch and screenshots - download and extract the archive.",
  dataset: "Preview isn't available for this dataset format - download to open it.",
  document: "Download to open the document in Word or your PDF reader.",
};

/**
 * The fallback floor every artifact can stand on: identity, provenance chips, and the download.
 * Reached when the server's render hint is `none`, when a preview fetch fails (`degraded`), or
 * when a renderer crashes (via RendererBoundary).
 */
export function DownloadCard({
  artifact,
  download,
  degraded = false,
}: {
  artifact: ArtifactOut;
  download: DownloadOut | null;
  degraded?: boolean;
}) {
  const note = degraded
    ? "The preview couldn't load - download the file instead."
    : (NO_PREVIEW[artifact.type] ?? "No preview for this file type - download to open it.");
  return (
    <div className="ring-foreground/10 bg-card flex items-center gap-3 rounded-lg p-4 ring-1">
      <IconTile icon={artifactIcon(artifact.type)} />
      <div className="min-w-0 flex-1">
        <p className="truncate text-body-sm-regular font-medium">{artifact.filename}</p>
        <p className="text-muted-foreground text-caption-md-regular">{note}</p>
        <p className="text-muted-foreground mt-1 flex flex-wrap gap-3 text-caption-md-regular">
          <MetaChip>{artifactKindLabel(artifact.type)}</MetaChip>
          <MetaChip>{formatBytes(artifact.bytes)}</MetaChip>
        </p>
      </div>
      {download && (
        <Button
          nativeButton={false}
          render={<a href={download.url} download={download.filename} />}
          variant="secondary"
          size="sm"
          stretch="auto"
          icon={<Icon icon={Download} />}
          label="Download"
        />
      )}
    </div>
  );
}
