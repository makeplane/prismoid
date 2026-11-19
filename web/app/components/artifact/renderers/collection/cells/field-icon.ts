/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import {
  Calendar,
  CheckSquare,
  CircleUserRound,
  Hash,
  Link2,
  List,
  Tags,
  Type,
} from "lucide-react";
import type { CollectionField } from "~/lib/collection";

/** Notion's quiet grammar: one glyph per field type, everywhere - table headers, the toolbar's
 * field menus, the record peek's labels - so the schema is readable without opening a cell. */
export const TYPE_ICON: Record<CollectionField["type"], typeof Type> = {
  text: Type,
  number: Hash,
  date: Calendar,
  select: List,
  multi_select: Tags,
  checkbox: CheckSquare,
  url: Link2,
  person: CircleUserRound,
};
