/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The settings dialog's panes. Each pane fetches its own data on mount (dialog panels
// have no route loader); mutations whose response carries the row land locally, the rest
// reload - the same content the standalone /settings, /skills and /models pages carried
// before they folded into the dialog.

export { ApiKeysPanel } from "./settings/api-keys-panel";
export { ConnectorRowItem, ConnectorsPanel } from "./settings/connectors-panel";
export { GeneralPanel } from "./settings/general-panel";
export { MemoryPanel, userMemoryMeta } from "./settings/memory-panel";
export { SkillsPanel } from "./settings/skills-panel";
export { UsagePanel } from "./settings/usage-panel";
