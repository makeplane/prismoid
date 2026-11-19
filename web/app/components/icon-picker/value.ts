/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/** The icon wire vocabulary - one place that knows the `lucide:<slug>` / `emoji:<char>`
 *  strings the server stores (and never interprets). Everything else speaks IconValue. */

export type IconValue = { kind: "lucide"; name: string } | { kind: "emoji"; char: string };

export function parseIcon(wire: string | null | undefined): IconValue | null {
  if (!wire) return null;
  if (wire.startsWith("lucide:")) return { kind: "lucide", name: wire.slice(7) };
  if (wire.startsWith("emoji:")) return { kind: "emoji", char: wire.slice(6) };
  return null; // an unknown prefix renders as the default face, never crashes
}

export function formatIcon(value: IconValue): string {
  return value.kind === "lucide" ? `lucide:${value.name}` : `emoji:${value.char}`;
}
