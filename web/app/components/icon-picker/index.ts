/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The icon-picker feature - a project's face, end to end: the wire vocabulary (value.ts),
// the curated catalogs (catalog.ts, emoji.ts - data, not logic), the display atom
// (ProjectIcon), and the picker (IconPicker, content-only). Consumers import from here.
export { IconPicker } from "./icon-picker";
export { ProjectIcon } from "./project-icon";
export { BotAvatar, nameHue } from "./bot-avatar";
export { formatIcon, parseIcon, type IconValue } from "./value";
