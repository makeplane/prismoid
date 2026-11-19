/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/** ONE canonical organizer stream for the draft-breakdown suites - draft-shape.test.ts
 *  cuts it at byte offsets to pin the closing-quote law; route.test.tsx renders it whole.
 *  A schema tweak is one edit here, not a drift between two hand-built copies. (Not a
 *  .test file: importing a test file into another suite re-registers its tests.) */
export const COLLECTION_STREAM = `{"filename": "launch-tracker", "summary": "Your launches, organized in one board.", "collection": {"collection_version": 1, "title": "Launch tracker", "fields": [{"key": "name", "label": "Name", "type": "text"}, {"key": "stage", "label": "Stage", "type": "select", "options": ["Planned", "Shipped"]}], "rows": [{"id": "r1", "cells": {"name": "Metro-North rail extension", "stage": "Planned"}}, {"id": "r2", "cells": {"name": "Q3 launch email", "stage": "Shipped"}}], "views": [{"name": "Board", "layout": "board", "group_by": "stage"}, {"name": "All", "layout": "table"}]}}`;
