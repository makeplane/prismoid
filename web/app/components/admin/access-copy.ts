/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// Pure copy for the user detail page's Access tab - device lines and expiry clocks. Kept out
// of the components so the words are testable without a DOM.

/**
 * The session row's device line: "Chrome on macOS", "Prismoid CLI", or - when the string is
 * something we do not recognise - the raw user agent, which is at least true.
 *
 * This used to be `ua || "Unknown device"`, which is not a device line; it put a 140-character
 * Mozilla incantation in a column labelled "device" and let CSS truncate it to nonsense. An
 * operator reading the Access tab is answering "is that session the person's laptop or a stale
 * CI token", and the raw string does not answer it.
 *
 * Deliberately small: a handful of families, most-specific first (Edge and Chrome both claim
 * "Chrome"; every browser on earth claims "Mozilla"). It is a legibility aid, not fingerprinting
 * - when the guess would be a guess, the truth goes through unchanged.
 */
const CLIENTS: [RegExp, string][] = [
  // Our own clients first - an operator most wants to tell these apart from a browser.
  [/\bprismoid-cli\b/i, "Prismoid CLI"],
  [/\bPrismoid\b/i, "Prismoid desktop app"],
  [/\bElectron\b/i, "Prismoid desktop app"],
  [/\b(curl|wget|httpie|python-requests|httpx|node-fetch|go-http-client)\b/i, "Script or tool"],
];

const BROWSERS: [RegExp, string][] = [
  [/\bEdg[eA]?\//, "Edge"],
  [/\bOPR\/|\bOpera\//, "Opera"],
  [/\bFirefox\//, "Firefox"],
  [/\bChrome\//, "Chrome"],
  [/\bSafari\//, "Safari"],
];

const PLATFORMS: [RegExp, string][] = [
  [/\biPhone\b/, "iPhone"],
  [/\biPad\b/, "iPad"],
  [/\bAndroid\b/, "Android"],
  [/\bMac OS X\b|\bMacintosh\b/, "macOS"],
  [/\bWindows NT\b/, "Windows"],
  [/\bCrOS\b/, "ChromeOS"],
  [/\bLinux\b/, "Linux"],
];

const firstMatch = (table: [RegExp, string][], ua: string): string | null =>
  table.find(([pattern]) => pattern.test(ua))?.[1] ?? null;

export function deviceLine(userAgent: string): string {
  const ua = userAgent.trim();
  if (!ua) return "Unknown device";

  const client = firstMatch(CLIENTS, ua);
  const platform = firstMatch(PLATFORMS, ua);
  if (client) return platform ? `${client} on ${platform}` : client;

  const browser = firstMatch(BROWSERS, ua);
  if (browser) return platform ? `${browser} on ${platform}` : browser;

  return ua; // unrecognised: the raw string, rather than a confident wrong answer
}

const rel = (s: number): string => {
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h`;
  return `${Math.floor(h / 24)}d`;
};

/** "expires in 6d" while the clock runs; "expired 3h ago" once it has. */
export function expiryLabel(expiresAtIso: string, nowMs = Date.now()): string {
  const s = Math.floor((Date.parse(expiresAtIso) - nowMs) / 1000);
  if (s <= 0) return `expired ${rel(-s)} ago`;
  return `expires in ${rel(s)}`;
}
