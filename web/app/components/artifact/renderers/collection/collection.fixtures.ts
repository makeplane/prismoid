/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/** One fixture, shared by the layout tests and the /ds gallery - the same data seen
 * through every lens, which is the collection kind's whole thesis. */

export const FIXTURE = {
  collection_version: 1,
  title: "Q3 workstreams",
  fields: [
    { key: "name", label: "Name", type: "text" },
    { key: "stage", label: "Stage", type: "select", options: ["Todo", "Doing", "Done"] },
    { key: "start", label: "Start", type: "date" },
    { key: "due", label: "Due", type: "date" },
    { key: "owner", label: "Owner", type: "person" },
    { key: "site", label: "Site", type: "url" },
    { key: "cost", label: "Cost", type: "number", unit: "$" },
    { key: "shipped", label: "Shipped", type: "checkbox" },
  ],
  rows: [
    {
      id: "r1",
      cells: {
        name: "Line expansion",
        stage: "Doing",
        start: "2026-07-01",
        due: "2026-08-14",
        owner: "Dana Whitfield",
        site: "https://example.com/line",
        cost: 41.6,
        shipped: false,
      },
    },
    {
      id: "r2",
      cells: {
        name: "Field-engineer hiring",
        stage: "Todo",
        start: "2026-08-01",
        due: "2026-09-15",
        owner: "Mia Chen",
      },
    },
    {
      id: "r3",
      cells: {
        name: "Escalation runbook",
        stage: "Done",
        start: "2026-06-10",
        due: "2026-07-02",
        owner: "Dana Whitfield",
        shipped: true,
      },
    },
    { id: "r4", cells: { name: "Vendor consolidation", cost: 12 } },
  ],
  views: [
    { name: "Pipeline", layout: "board", group_by: "stage", column_width: 320 },
    {
      name: "All",
      layout: "table",
      columns: ["name", "stage", "owner", "due", "cost"],
      widths: { name: 320 },
    },
    { name: "Timeline", layout: "gantt", start_field: "start", end_field: "due" },
    { name: "Deadlines", layout: "calendar", date_field: "due" },
    { name: "Gallery", layout: "gallery", cover_field: "site" },
    { name: "List", layout: "list", sort: [{ field: "due", direction: "asc" }] },
  ],
};

export const FIXTURE_TEXT = JSON.stringify(FIXTURE);
