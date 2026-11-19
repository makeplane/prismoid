/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { Collection } from "~/lib/collection";
import { parseCollection } from "~/lib/collection";
import type { ArtifactOut, DownloadOut } from "../registry";
import { BoardView } from "./board/board-view";
import { CalendarView } from "./calendar/calendar-view";
import CollectionRenderer from "./collection-view";
import { FIXTURE, FIXTURE_TEXT } from "./collection.fixtures";
import { DAY_MS } from "./core/date-math";
import { GalleryView } from "./gallery/gallery-view";
import { fitAxis, initialAxis, ZOOMS } from "./gantt/axis";
import { RAIL_W } from "./gantt/bars";
import { GanttView } from "./gantt/gantt-view";
import { ListView } from "./list/list-view";
import { TableView } from "./table/table-view";

const D = (iso: string) => Date.parse(`${iso}T00:00:00Z`);

const DATA = (parseCollection(FIXTURE_TEXT) as { ok: true; data: Collection }).data;
const view = (name: string) => DATA.views.find((v) => v.name === name)!;

const artifact = {
  id: "a1",
  type: "collection",
  title: "Q3 workstreams",
  filename: "q3.collection.json",
  render: "collection",
  sha256: "0".repeat(64),
  bytes: 1,
  provenance: {},
  verdict: null,
  created_at: "2026-07-31T00:00:00Z",
} as ArtifactOut;
const download: DownloadOut = {
  url: "https://blobs.example/q3",
  filename: "q3.collection.json",
  sha256: "0".repeat(64),
  expires_in: 3600,
};

describe("CollectionRenderer", () => {
  it("renders the switcher in the artifact's view order and lands on the first lens", () => {
    const html = renderToStaticMarkup(
      <CollectionRenderer artifact={artifact} download={download} text={FIXTURE_TEXT} />,
    );
    for (const v of DATA.views) expect(html).toContain(v.name);
    expect(html).toContain("4 rows");
    expect(html).toContain('aria-label="Pipeline board"'); // first view = board
  });

  it("degrades invalid content to an honest card with the download escape hatch", () => {
    const html = renderToStaticMarkup(
      <CollectionRenderer artifact={artifact} download={download} text="{broken" />,
    );
    expect(html).toContain("can&#x27;t be read");
    expect(html).toContain('href="https://blobs.example/q3"');
  });

  it("degrades an unknown layout to the table with a note, never a rejection", () => {
    const doc = { ...FIXTURE, views: [{ name: "New", layout: "starmap" }] };
    const html = renderToStaticMarkup(
      <CollectionRenderer artifact={artifact} download={download} text={JSON.stringify(doc)} />,
    );
    expect(html).toContain("starmap");
    expect(html).toContain("<table");
  });
});

describe("BoardView", () => {
  it("buckets rows into option-ordered columns plus the honest tail", () => {
    const html = renderToStaticMarkup(<BoardView data={DATA} view={view("Pipeline")} />);
    for (const label of ["Todo", "Doing", "Done", "No Stage"]) expect(html).toContain(label);
    expect(html).toContain("Line expansion");
    expect(html).toContain("Vendor consolidation");
  });

  it("card titles clamp as BLOCKS - the inline-span nowrap that bled across columns is banned", () => {
    const html = renderToStaticMarkup(<BoardView data={DATA} view={view("Pipeline")} />);
    expect(html).toContain("line-clamp-2");
    // The old bleeding recipe was an inline span whose ONLY layout classes were these:
    expect(html).not.toContain('class="truncate text-body-sm-regular font-medium"');
  });

  it("column headers center pill and count on ONE line - the h3 is a flex box, not a text box", () => {
    // An inline-flex Badge inside a plain block <h3> sits on the text BASELINE and inflates
    // the line box with phantom descent (the table-cell disease) - the pill drifts off the
    // count's centerline. As a flex container the pill is an item and centers exactly.
    const html = renderToStaticMarkup(<BoardView data={DATA} view={view("Pipeline")} />);
    expect(html).toContain('<h3 class="flex min-w-0 items-center">');
  });

  it("cards carry the view's visible fields as LABELED typed chips, never bare numerals", () => {
    const html = renderToStaticMarkup(<BoardView data={DATA} view={view("Pipeline")} />);
    expect(html).toContain("Cost"); // a number chip leads with its field label
    expect(html).toContain("$41.6");
  });

  it("columns take the document's shared width and offer resize handles only to savers", () => {
    const readOnly = renderToStaticMarkup(<BoardView data={DATA} view={view("Pipeline")} />);
    expect(readOnly).toContain("width:320px"); // view.column_width from the fixture
    expect(readOnly).not.toContain('aria-label="Resize board columns"');
    const editable = renderToStaticMarkup(
      <BoardView data={DATA} view={view("Pipeline")} onUpdateView={() => {}} />,
    );
    expect((editable.match(/aria-label="Resize board columns"/g) ?? []).length).toBe(4);
  });

  it("a selected card wears the selection ring (the ONE law: ring-2 ring-primary/40)", () => {
    const selected = renderToStaticMarkup(
      <BoardView data={DATA} view={view("Pipeline")} selectedRowId="r1" onSelectRow={() => {}} />,
    );
    expect((selected.match(/ring-primary\/40/g) ?? []).length).toBe(1);
    const plain = renderToStaticMarkup(<BoardView data={DATA} view={view("Pipeline")} />);
    expect(plain).not.toContain("ring-primary/40");
  });
});

describe("TableView", () => {
  it("respects the view's column subset and marks sortable headers", () => {
    const html = renderToStaticMarkup(<TableView data={DATA} view={view("All")} />);
    expect(html).toContain(">Name<");
    expect(html).toContain(">Cost<");
    expect(html).not.toContain(">Shipped<"); // not in columns[]
    expect(html).toContain("$41.6");
  });
});

describe("GanttView", () => {
  it("draws one bar per dated row - tinted by the select field - and lists the undated honestly", () => {
    const html = renderToStaticMarkup(<GanttView data={DATA} view={view("Timeline")} />);
    // v4: bars wear the stage option's hue (r1 Doing→hue-2, r2 Todo→hue-1, r3 Done→hue-3).
    expect((html.match(/bg-hue-\d/g) ?? []).length).toBe(3);
    expect(html).toContain("No dates (1)");
    expect(html).toContain("Vendor consolidation");
  });
});

describe("CalendarView", () => {
  it("lands rows on their due day in the seeded month", () => {
    // Seed month = latest due (Sep 2026); r2 due 2026-09-15 must appear as a chip.
    const html = renderToStaticMarkup(<CalendarView data={DATA} view={view("Deadlines")} />);
    expect(html).toContain("September 2026");
    expect(html).toContain("Field-engineer hiring");
  });
});

describe("GalleryView", () => {
  it("never auto-loads a cover from another origin: a quiet tile with a click-through chip", () => {
    const html = renderToStaticMarkup(<GalleryView data={DATA} view={view("Gallery")} />);
    expect(html).not.toContain('src="https://example.com/line"');
    expect(html).toContain('href="https://example.com/line"');
    expect(html).toContain('target="_blank"');
  });

  it("loads a cover from an origin the image policy allows", () => {
    const own = {
      ...DATA,
      rows: DATA.rows.map((r) => ({ ...r, cells: { ...r.cells, site: "/media/line.png" } })),
    };
    const html = renderToStaticMarkup(<GalleryView data={own} view={view("Gallery")} />);
    expect(html).toContain('src="/media/line.png"');
    expect(html).toContain('referrerPolicy="no-referrer"');
  });
});

describe("TableView v3 (colgroup widths + resize handles)", () => {
  it("lays out through colgroup under table-fixed, one col per visible field", () => {
    const html = renderToStaticMarkup(<TableView data={DATA} view={view("All")} />);
    expect(html).toContain("table-fixed");
    expect((html.match(/<col /g) ?? []).length).toBe(5); // name stage owner due cost
    // a plain <table> under a bare root: no overflow wrapper between the shell scroller and
    // the sticky header, so it resolves against the shell scroller.
    expect(html).toMatch(/^<div><table /);
  });

  it("reads persisted view.widths and falls back to type defaults elsewhere", () => {
    const html = renderToStaticMarkup(<TableView data={DATA} view={view("All")} />);
    expect(html).toContain("width:320px"); // widths: { name: 320 } from the fixture
    expect(html).toContain("width:112px"); // cost - the number default
  });

  it("renders resize separators only when the view can save", () => {
    const readOnly = renderToStaticMarkup(<TableView data={DATA} view={view("All")} />);
    expect(readOnly).not.toContain('role="separator"');
    const editable = renderToStaticMarkup(
      <TableView data={DATA} view={view("All")} onUpdateView={() => {}} />,
    );
    expect((editable.match(/role="separator"/g) ?? []).length).toBe(5);
    expect(editable).toContain('aria-label="Resize Name"');
  });
});

describe("TableView regressions", () => {
  it("NEVER puts content-visibility on table rows - rows cannot be size-contained", () => {
    // The smeared-table bug: contain-intrinsic-size on a <tr> collapses every row onto one
    // line. The skip-paint pattern belongs to the board/list block boxes only.
    const html = renderToStaticMarkup(<TableView data={DATA} view={view("All")} />);
    expect(html).not.toContain("content-visibility");
  });

  it("rows sit ON the lens grid: single-line cells, rail-twin primary, scalar nowrap", () => {
    // 17px root makes rem padding + chips overflow a 44px row, so table rows must not derive
    // height from content: tr pins h-(--row-h) (a floor on <tr>), cells center (align-middle),
    // prose truncates to one line - the primary in the gantt rail title's exact type.
    const html = renderToStaticMarkup(<TableView data={DATA} view={view("All")} />);
    expect(html).toContain("h-(--row-h)");
    expect(html).toContain("truncate text-body-sm-regular leading-tight"); // primary = rail title twin
    expect(html).not.toContain("line-clamp-2"); // full text lives in the peek
    expect(html).not.toContain("align-top");
    expect(html).toContain("whitespace-nowrap");
    expect(html).not.toContain("<dl"); // the row peek opens on click, never by default
  });
});

describe("v4 drag affordances (per-lens, saveable vs reader)", () => {
  it("table: gutter grips render only when saveable AND unsorted; separators stay 5", () => {
    const editable = renderToStaticMarkup(
      <TableView data={DATA} view={view("All")} onUpdateView={() => {}} onMoveRow={() => {}} />,
    );
    expect((editable.match(/<col /g) ?? []).length).toBe(6); // gutter + 5 fields
    expect((editable.match(/aria-label="Drag to reorder"/g) ?? []).length).toBe(4);
    expect(editable).toContain('aria-roledescription="sortable"');
    expect((editable.match(/role="separator"/g) ?? []).length).toBe(5); // grips are buttons

    const readOnly = renderToStaticMarkup(<TableView data={DATA} view={view("All")} />);
    expect((readOnly.match(/<col /g) ?? []).length).toBe(5);
    expect(readOnly).not.toContain("Drag to reorder");
  });

  it("table: an active sort hides the grips and says why", () => {
    const sorted = { ...view("All"), sort: [{ field: "due", direction: "asc" as const }] };
    const html = renderToStaticMarkup(
      <TableView data={DATA} view={sorted} onUpdateView={() => {}} onMoveRow={() => {}} />,
    );
    expect(html).not.toContain("Drag to reorder");
    expect(html).toContain("Clear sort to reorder");
  });

  it("list: grips when flat+unsorted; grouped lists trade grips for section pills", () => {
    const flat = { ...view("List"), sort: [] };
    const editable = renderToStaticMarkup(
      <ListView data={DATA} view={flat} onMoveRow={() => {}} />,
    );
    expect(editable).toContain("Drag to reorder");

    const grouped = { ...flat, group_by: "stage" };
    const sections = renderToStaticMarkup(
      <ListView data={DATA} view={grouped} onMoveRow={() => {}} />,
    );
    expect(sections).not.toContain("Drag to reorder");
    expect(sections).toContain("bg-label-purple-bg"); // the Todo section pill wears its hue
  });

  it("gallery: whole-tile reorder when saveable, nothing for readers", () => {
    const noSort = { ...view("Gallery"), sort: [] };
    const html = renderToStaticMarkup(
      <GalleryView data={DATA} view={noSort} onMoveRow={() => {}} />,
    );
    expect(html).toContain("cursor-grab");
    expect(html).toContain('aria-roledescription="sortable"');
    const readOnly = renderToStaticMarkup(<GalleryView data={DATA} view={noSort} />);
    expect(readOnly).not.toContain("cursor-grab");
  });

  it("calendar: chips drag when saveable", () => {
    const editable = renderToStaticMarkup(
      <CalendarView data={DATA} view={view("Deadlines")} onSetCell={() => {}} />,
    );
    expect(editable).toContain('aria-roledescription="draggable"');
    expect(editable).toContain("cursor-grab");
    const readOnly = renderToStaticMarkup(<CalendarView data={DATA} view={view("Deadlines")} />);
    expect(readOnly).not.toContain("cursor-grab");
  });

  it("gantt: 3 grabbable bars and 6 edge handles when saveable, zero for readers", () => {
    const html = renderToStaticMarkup(
      <GanttView data={DATA} view={view("Timeline")} onMoveBar={() => {}} onResizeBar={() => {}} />,
    );
    expect((html.match(/cursor-ew-resize/g) ?? []).length).toBe(6);
    expect((html.match(/cursor-grab/g) ?? []).length).toBe(3);
    const readOnly = renderToStaticMarkup(<GanttView data={DATA} view={view("Timeline")} />);
    expect(readOnly).not.toContain("cursor-ew-resize");
    expect(readOnly).not.toContain("cursor-grab");
  });

  it("gantt: the rail carries the table's grip grammar - reorder when unsorted, hidden when sorted/grouped", () => {
    const withGrips = renderToStaticMarkup(
      <GanttView data={DATA} view={view("Timeline")} onMoveRow={() => {}} />,
    );
    expect((withGrips.match(/aria-label="Drag to reorder"/g) ?? []).length).toBe(3);

    const sorted = { ...view("Timeline"), sort: [{ field: "due", direction: "asc" as const }] };
    const noGrips = renderToStaticMarkup(
      <GanttView data={DATA} view={sorted} onMoveRow={() => {}} />,
    );
    expect(noGrips).not.toContain("Drag to reorder");

    const grouped = { ...view("Timeline"), group_by: "stage" };
    const laned = renderToStaticMarkup(
      <GanttView data={DATA} view={grouped} onMoveRow={() => {}} />,
    );
    expect(laned).not.toContain("Drag to reorder");
  });

  it("board: cards stay cursor-grab; the overlay contributes nothing at rest", () => {
    const html = renderToStaticMarkup(
      <BoardView data={DATA} view={view("Pipeline")} onDropCard={() => {}} />,
    );
    expect(html).toContain("cursor-grab");
    expect(html).not.toContain("cursor-grabbing");
  });
});

describe("view naming", () => {
  it("savers get the double-click rename affordance on chips; readers get none", () => {
    const editable = renderToStaticMarkup(
      <CollectionRenderer
        artifact={artifact}
        download={download}
        text={FIXTURE_TEXT}
        onArtifactChanged={() => {}}
      />,
    );
    expect(editable).toContain('title="Double-click to rename"');
    const reader = renderToStaticMarkup(
      <CollectionRenderer artifact={artifact} download={download} text={FIXTURE_TEXT} />,
    );
    expect(reader).not.toContain("Double-click to rename");
  });
});

describe("v4 grouping + option color", () => {
  it("board column pills and select badges wear the option hue", () => {
    const html = renderToStaticMarkup(<BoardView data={DATA} view={view("Pipeline")} />);
    expect(html).toContain("bg-label-purple-bg"); // Todo (option index 0)
    expect(html).toContain("bg-label-indigo-bg"); // Doing
  });

  it("gantt swimlanes render option-pill lane headers when grouped; bars stay 3", () => {
    const grouped = { ...view("Timeline"), group_by: "stage" };
    const html = renderToStaticMarkup(<GanttView data={DATA} view={grouped} />);
    expect((html.match(/bg-muted\/20/g) ?? []).length).toBe(3); // Todo/Doing/Done lanes
    expect((html.match(/bg-hue-\d /g) ?? []).length).toBe(3); // solid-tinted bars
    const flat = renderToStaticMarkup(<GanttView data={DATA} view={view("Timeline")} />);
    expect(flat).not.toContain("bg-muted/20");
  });

  it("the Group menu reaches gantt views", () => {
    const doc = {
      ...FIXTURE,
      views: [{ name: "Timeline", layout: "gantt", start_field: "start", end_field: "due" }],
    };
    const html = renderToStaticMarkup(
      <CollectionRenderer
        artifact={artifact}
        download={download}
        text={JSON.stringify(doc)}
        onArtifactChanged={() => {}}
      />,
    );
    expect(html).toContain('aria-label="Group"');
  });
});

describe("ViewToolbar (v3)", () => {
  it("the Filter · Sort · Fields (· Group) engine appears for savers and never for readers", () => {
    const editable = renderToStaticMarkup(
      <CollectionRenderer
        artifact={artifact}
        download={download}
        text={FIXTURE_TEXT}
        onArtifactChanged={() => {}}
      />,
    );
    for (const label of ["Filter", "Sort", "Fields", "Group"]) {
      expect(editable).toContain(`aria-label="${label}"`); // first view is a board → Group too
    }
    const readOnly = renderToStaticMarkup(
      <CollectionRenderer artifact={artifact} download={download} text={FIXTURE_TEXT} />,
    );
    expect(readOnly).not.toContain('aria-label="Filter"');
    expect(readOnly).toContain("4 rows"); // meta stays for everyone
  });
});

describe("ViewShell (v3)", () => {
  it("wraps every lens in the one surface: a labeled region, visible scrollbars, no peek by default", () => {
    const html = renderToStaticMarkup(
      <CollectionRenderer artifact={artifact} download={download} text={FIXTURE_TEXT} />,
    );
    expect(html).toContain('role="region"');
    expect(html).toContain("scrollbar-thin");
    expect(html).not.toContain('aria-label="Record"'); // the peek opens on selection only
  });

  it("the scroll contract: document flow is FULL height - the page scrolls, never the shell", () => {
    // The viewport cap was the scroll-in-a-scroll: a board bounded to
    // `max-h-[calc(100dvh-15rem)]` scrolled inside itself while the page scrolled around
    // it, and the user named it ugly twice. Document flow now hugs the whole board - the
    // ONLY vertical scroller is the page/pane around it; the internal scroller pans wide
    // layouts horizontally. No h-full either: the shell must never size to a host box.
    const doc = renderToStaticMarkup(
      <CollectionRenderer artifact={artifact} download={download} text={FIXTURE_TEXT} />,
    );
    expect(doc).not.toContain("max-h-[calc(100dvh-15rem)]");
    expect(doc).not.toContain("h-full");
    // …and the wheel passes THROUGH the board: the shell scroller owns the horizontal
    // axis only. `overflow-auto overscroll-contain` ate every vertical wheel over the
    // board (nothing left to scroll + chaining blocked → page frozen under the cursor).
    expect(doc).toContain("overflow-x-auto");
    expect(doc).toContain("overflow-y-hidden");
    expect(doc).not.toContain("overflow-auto ");
  });

  it("an empty projection is an honest EmptyState, never a bare column skeleton", () => {
    const doc = { ...FIXTURE, rows: [] };
    const html = renderToStaticMarkup(
      <CollectionRenderer artifact={artifact} download={download} text={JSON.stringify(doc)} />,
    );
    expect(html).toContain("No rows");
  });
});

describe("direct manipulation affordances", () => {
  it("a saveable viewer gets add-view, new-row, and draggable cards; a reader gets none", () => {
    const editable = renderToStaticMarkup(
      <CollectionRenderer
        artifact={artifact}
        download={download}
        text={FIXTURE_TEXT}
        onArtifactChanged={() => {}}
      />,
    );
    expect(editable).toContain('aria-label="Add view"');
    expect(editable).toContain("cursor-grab"); // board cards drag directly - no edit mode

    const readOnly = renderToStaticMarkup(
      <CollectionRenderer artifact={artifact} download={download} text={FIXTURE_TEXT} />,
    );
    expect(readOnly).not.toContain('aria-label="Add view"');
    expect(readOnly).not.toContain("cursor-grab");
    expect(readOnly).not.toContain("Save revision"); // the pill appears only once edits stage
  });
});

describe("GanttView v2", () => {
  it("draws the two-band axis: month bands, week ticks, weekend shading, and the rail range", () => {
    const html = renderToStaticMarkup(<GanttView data={DATA} view={view("Timeline")} />);
    expect(html).toContain("Jun 2026"); // month band
    expect(html).toContain("Jun 10"); // rail range line start (r3)
    expect(html).toContain("bg-muted/40"); // weekend shading bands + hover class family
    expect(html).toContain("3 scheduled");
    expect(html).toContain("· 23d"); // a bar's duration on the rail (r3: Jun 10 – Jul 2)
  });
});

describe("GanttView v3 (windowed infinite axis)", () => {
  it("keeps the rail and header sticky against the shell's one scroller", () => {
    const html = renderToStaticMarkup(<GanttView data={DATA} view={view("Timeline")} />);
    expect(html).toContain("sticky left-0");
    expect(html).toContain("sticky top-0");
  });

  it("fills the scrollport at open - the dead-space formula, exercised without a browser", () => {
    const axis = fitAxis(
      initialAxis(D("2026-06-10"), D("2026-09-15"), ZOOMS.month.dayPx, 2400),
      ZOOMS.month.dayPx,
      2400,
    );
    const total = (axis.endMs - axis.startMs) / DAY_MS + 1;
    expect(total * ZOOMS.month.dayPx).toBeGreaterThanOrEqual(2400);
    const html = renderToStaticMarkup(
      <GanttView data={DATA} view={view("Timeline")} initialContainerWidth={2400} />,
    );
    expect(html).toContain(`width:${RAIL_W + total * ZOOMS.month.dayPx}px`);
  });

  it("renders the zoom dropdown and Today inline when no toolbar slot exists (the fallback)", () => {
    const html = renderToStaticMarkup(<GanttView data={DATA} view={view("Timeline")} />);
    expect(html).toContain('aria-label="Zoom"');
    expect(html).toContain("Month"); // the fixture's span auto-picks month zoom; the trigger shows it
    expect(html).toContain("Today");
  });

  it("a 1-day row at year zoom is an 8px pill, never a lost dot", () => {
    const twoYears = {
      ...FIXTURE,
      rows: [
        { id: "a", cells: { name: "Kickoff", start: "2026-01-05", due: "2026-01-05" } },
        { id: "b", cells: { name: "Launch", start: "2027-06-01", due: "2027-06-05" } },
      ],
    };
    const doc = (parseCollection(JSON.stringify(twoYears)) as { ok: true; data: Collection }).data;
    const html = renderToStaticMarkup(
      <GanttView data={doc} view={doc.views.find((v) => v.name === "Timeline")!} />,
    );
    expect(html).toContain("width:8px");
    expect(html).toContain("rounded-full");
  });

  it("zero dated rows is an honest EmptyState, not a bare paragraph", () => {
    const undatedOnly = { ...FIXTURE, rows: [{ id: "x", cells: { name: "Loose end" } }] };
    const doc = (parseCollection(JSON.stringify(undatedOnly)) as { ok: true; data: Collection })
      .data;
    const html = renderToStaticMarkup(
      <GanttView data={doc} view={doc.views.find((v) => v.name === "Timeline")!} />,
    );
    expect(html).toContain("Nothing scheduled");
  });
});
