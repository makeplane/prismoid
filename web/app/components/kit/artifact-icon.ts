/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// THE artifact vocabulary - one home for every kind's human name and glyph (the naming law,
// flattened): what people call the thing, never the wire string. One wire kind,
// one word - a `document` is a **Document** whether it is memo.md, report.docx, or page.html
// (the filename-earned "Page" split confused more than it clarified and is gone).
// deck → **Presentation** · webapp → **App** · collection → **Collection** - every chip,
// card, dial, and rail speaks from here. Legacy council_* rows ride the fleet glyph.
import {
  AppWindow,
  ChartColumn,
  Database,
  File,
  FileCode,
  FileDiff,
  FileSpreadsheet,
  FileText,
  Presentation,
  Table2,
} from "lucide-react";

const ARTIFACT_ICON: Record<string, typeof File> = {
  document: FileText,
  spreadsheet: FileSpreadsheet,
  deck: Presentation,
  code: FileCode,
  change: FileDiff,
  webapp: AppWindow,
  dataset: Database,
  collection: Table2,
  fleet_report: ChartColumn,
  council_report: ChartColumn,
  council_opinion: ChartColumn,
};

export function artifactIcon(type?: string | null): typeof File {
  return ARTIFACT_ICON[type ?? ""] ?? File;
}

const ARTIFACT_LABEL: Record<string, string> = {
  document: "Document",
  spreadsheet: "Spreadsheet",
  deck: "Presentation",
  code: "Code",
  change: "Change",
  webapp: "App",
  dataset: "Dataset",
  collection: "Collection",
  fleet_report: "Fleet report",
  council_report: "Fleet report",
  council_opinion: "Fleet opinion",
};

export function artifactKindLabel(type?: string | null): string {
  return ARTIFACT_LABEL[type ?? ""] ?? "Artifact";
}
