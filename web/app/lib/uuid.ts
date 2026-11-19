/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// One UUID v4, minted safely everywhere. `crypto.randomUUID` exists only in secure contexts
// (https, localhost): on a plain-http LAN address it is undefined, and a mint that runs inside an
// effect (the composer's text observer) would throw and take the whole page down - the same trap
// `mintConn` (lib/presence.ts) guards. The fallback builds a v4 from `getRandomValues`, which
// every context has; `Math.random` is the last resort. The shape always stays a UUID: the server
// parses a steer's `command_id` as one.

export function uuidV4(): string {
  const c: Crypto | undefined = globalThis.crypto;
  if (typeof c?.randomUUID === "function") return c.randomUUID();
  const bytes = new Uint8Array(16);
  if (typeof c?.getRandomValues === "function") c.getRandomValues(bytes);
  else for (let i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256);
  bytes[6] = (bytes[6] & 0x0f) | 0x40; // version 4
  bytes[8] = (bytes[8] & 0x3f) | 0x80; // RFC 4122 variant
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
