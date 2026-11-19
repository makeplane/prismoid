/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The breakdown must be built from an INCOMPLETE stream without ever lying: values only
// land once their closing quote does, the counter counts rows not noise, and the row
// tail names what just landed. These pin the closing-quote law, the head/tail split
// (same-named KEYS in rows can never shadow the top-level facts), and the schema-order
// fill (filename → summary → title → fields → rows → views).
import { describe, expect, it } from "vitest";
import { COLLECTION_STREAM as FULL } from "./collection-stream.fixture";
import { collectionDraftShape, RECENT_ROWS } from "./draft-shape";

describe("collectionDraftShape - structure out of a partial stream", () => {
  it("reads the whole document when it is all there", () => {
    const s = collectionDraftShape(FULL);
    expect(s.filename).toBe("launch-tracker");
    expect(s.summary).toBe("Your launches, organized in one board.");
    expect(s.title).toBe("Launch tracker");
    expect(s.fields).toEqual(["Name", "Stage"]);
    expect(s.rows).toBe(2);
    expect(s.recent).toEqual(["Metro-North rail extension", "Q3 launch email"]);
    expect(s.views).toEqual(["board", "table"]);
  });

  it("an early fragment shows nothing - no key mistaken for another", () => {
    const s = collectionDraftShape(`{"filename": "launch-tr`);
    expect(s).toEqual({
      filename: null,
      title: null,
      summary: null,
      fields: [],
      rows: 0,
      recent: [],
      views: [],
    });
  });

  it("the file gets its real name the moment the first key closes", () => {
    const cut = FULL.indexOf('"summary"');
    const s = collectionDraftShape(FULL.slice(0, cut));
    expect(s.filename).toBe("launch-tracker");
    expect(s.summary).toBeNull();
    expect(s.title).toBeNull();
  });

  it("a value lands only when its closing quote does - no half-words, ever", () => {
    const cut = FULL.indexOf("Launch tracker") + 6; // mid-title
    expect(collectionDraftShape(FULL.slice(0, cut)).title).toBeNull();
    const closed = FULL.indexOf('"fields"'); // just past the title
    expect(collectionDraftShape(FULL.slice(0, closed)).title).toBe("Launch tracker");
  });

  it("fields appear one by one, in schema order", () => {
    const oneField = FULL.indexOf('{"key": "stage"');
    expect(collectionDraftShape(FULL.slice(0, oneField)).fields).toEqual(["Name"]);
  });

  it("the tail names rows as their first cells complete - a row mid-stream keeps the previous tail", () => {
    const midSecondCell = FULL.indexOf("Q3 launch email") + 4; // second row's first cell, cut open
    const s = collectionDraftShape(FULL.slice(0, midSecondCell));
    expect(s.rows).toBe(2); // the second row's cells key already streamed…
    expect(s.recent).toEqual(["Metro-North rail extension"]); // …but its name has not closed
  });

  it("a rank-first table keeps its heartbeat - the tail takes the first STRING cell", () => {
    const s = collectionDraftShape(
      `{"collection": {"title": "Top Scientists", "rows": [` +
        `{"id": "r1", "cells": {"rank": 1, "scientist": "Marie Curie", "country": "Poland"}}, ` +
        `{"id": "r2", "cells": {"rank": 2, "scientist": "Max Planck", "country": "Germany"}}]}}`,
    );
    expect(s.rows).toBe(2);
    expect(s.recent).toEqual(["Marie Curie", "Max Planck"]);
  });

  it("a collection ABOUT titles/labels never shadows the card - keys in rows stay in rows", () => {
    const s = collectionDraftShape(
      `{"collection": {"rows": [{"id": "r1", "cells": {"title": "Dune", "label": "Sci-fi"}}]}}`,
    );
    expect(s.title).toBeNull(); // the row's "title" cell is not the collection's title
    expect(s.fields).toEqual([]); // the row's "label" cell is not a column
    expect(s.recent).toEqual(["Dune"]); // it IS, honestly, the row's first string cell
  });

  it(`the tail keeps only the last ${RECENT_ROWS}, oldest first`, () => {
    const rows = ["Curie", "Planck", "Noether", "Turing", "Franklin"]
      .map((n, i) => `{"id": "r${i}", "cells": {"name": "${n}"}}`)
      .join(", ");
    const s = collectionDraftShape(`{"collection": {"rows": [${rows}]}}`);
    expect(s.rows).toBe(5);
    expect(s.recent).toEqual(["Noether", "Turing", "Franklin"]);
  });

  it("views are bounded to the real layout enum - a stray 'layout' key mints nothing", () => {
    const s = collectionDraftShape(
      `{"collection": {"rows": [{"id": "r1", "cells": {"layout": "weird"}}], "views": [{"name": "Board", "layout": "board"}]}}`,
    );
    expect(s.views).toEqual(["board"]);
  });

  it("display-unescapes the common escapes, \\uXXXX included", () => {
    const s = collectionDraftShape(
      `{"collection": {"title": "The \\"big\\" board", "rows": [{"id": "r1", "cells": {"name": "Schr\\u00f6dinger \\\\ co"}}]}}`,
    );
    expect(s.title).toBe('The "big" board');
    expect(s.recent).toEqual(["Schrödinger \\ co"]);
  });
});
