/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/** Compact relative age, e.g. "3m ago". */
export function ago(iso: string): string {
  const s = Math.max(0, Math.floor((Date.now() - Date.parse(iso)) / 1000));
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

/** Compact FUTURE distance, e.g. "in 12m" - `ago`'s mirror (routines' next-fire column). */
export function inAgo(iso: string): string {
  const s = Math.floor((Date.parse(iso) - Date.now()) / 1000);
  if (s <= 0) return "now";
  if (s < 60) return `in ${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `in ${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `in ${h}h`;
  return `in ${Math.floor(h / 24)}d`;
}

/** A dollar figure people read at a glance: cents from a dollar up, three places below a dime
 *  (where a task's spend lives), and "<$0.001" for a sliver - "$0.000164" was a ledger line,
 *  not a price. The exact value stays the server's; this only formats it. */
export function formatUsd(n: number): string {
  if (n === 0) return "$0.00";
  if (n > 0 && n < 0.001) return "<$0.001";
  return `${n < 0 ? "-" : ""}$${Math.abs(n).toFixed(Math.abs(n) < 0.1 ? 3 : 2)}`;
}

const pad2 = (n: number) => String(n).padStart(2, "0");

/** The LOCAL calendar-day key (ISO-shaped so string sort IS chronology). */
export const dayKey = (d: Date): string =>
  `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;

/** Humanize a local day: Today, Yesterday, then dates (year only when it differs).
 *  Promoted on its second consumer (task-browser date grouping + the sidebar's). */
export function dayLabel(d: Date): string {
  const now = new Date();
  const key = dayKey(d);
  if (key === dayKey(now)) return "Today";
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (key === dayKey(yesterday)) return "Yesterday";
  return d.toLocaleDateString(undefined, {
    day: "numeric",
    month: "short",
    ...(d.getFullYear() !== now.getFullYear() ? { year: "numeric" } : {}),
  });
}

/** Human file size from a byte count. */
/** "Jul 31, 2026, 6:58 PM" - meta-line timestamps: absolute, minute precision, no seconds. */
export const formatDateTime = (iso: string): string =>
  new Date(iso).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

/** A person's name as the page draws it. Names are data the person controls:
 *  whitespace collapses to single spaces (no newline can break a line), and past 60 characters
 *  it ends in an ellipsis. React escapes the text; this only keeps the line honest. */
export function personName(name: string | null | undefined): string {
  const flat = (name ?? "").replace(/\s+/g, " ").trim();
  if (!flat) return "Someone";
  return flat.length > 60 ? `${flat.slice(0, 59).trimEnd()}…` : flat;
}
