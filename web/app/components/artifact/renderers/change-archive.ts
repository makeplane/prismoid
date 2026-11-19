/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// A `change` artifact is a plain tar: report.md, patch.diff,
// test-output.txt and screenshots/*.png in one folder. There is no per-file artifact URL, so the
// viewer reads the archive the one download link hands it. Every byte is the agent's: the names
// and sizes are checked against the buffer, only regular files count, and the caps match the
// verifier's (backend `ai/policy/verifier/change.py`). Pure: no fetch, no DOM.

const BLOCK = 512;
const PNG_SIGNATURE = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];
const FOLDER = "screenshots/";
const decoder = new TextDecoder();

export const MAX_ARCHIVE_BYTES = 64 * 1024 * 1024; // past this the download is the honest door
export const MAX_TEXT_BYTES = 1024 * 1024;
export const MAX_SCREENSHOTS = 50;
export const MAX_SCREENSHOT_BYTES = 10 * 1024 * 1024;
export const MAX_MEMBERS = 10_000;

export type ChangeShot = { name: string; bytes: Uint8Array<ArrayBuffer> };

export type ChangeArchive = {
  report: string | null;
  diff: string | null;
  testOutput: string | null;
  screenshots: ChangeShot[];
  notes: string[]; // what the caps left out, said on the page
};

function field(block: Uint8Array, at: number, length: number): string {
  const bytes = block.subarray(at, at + length);
  const end = bytes.indexOf(0);
  return decoder.decode(end === -1 ? bytes : bytes.subarray(0, end));
}

function octal(block: Uint8Array, at: number, length: number): number {
  const value = Number.parseInt(field(block, at, length).trim(), 8);
  if (!Number.isSafeInteger(value) || value < 0) throw new Error("not a tar");
  return value;
}

function vetted(block: Uint8Array): void {
  // The stored sum counts its own eight bytes as spaces.
  const stored = octal(block, 148, 8);
  const sum = block.reduce((total, byte, i) => total + (i >= 148 && i < 156 ? 32 : byte), 0);
  if (sum !== stored) throw new Error("not a tar");
}

/** One record of a pax extended header: `<length> <key>=<value>\n`. */
function paxPath(body: Uint8Array): string | null {
  let at = 0;
  while (at < body.length) {
    const space = body.indexOf(0x20, at);
    if (space === -1) return null;
    const length = Number.parseInt(decoder.decode(body.subarray(at, space)), 10);
    if (!Number.isSafeInteger(length) || length <= 0) return null;
    const record = decoder.decode(body.subarray(space + 1, at + length - 1));
    if (record.startsWith("path=")) return record.slice("path=".length);
    at += length;
  }
  return null;
}

function safe(name: string): boolean {
  return name !== "" && !name.startsWith("/") && !name.split("/").includes("..");
}

/** The tar's regular files, by name relative to the project (publish wraps it in one folder).
 *  Throws on bytes that are not a tar. `onCapped` hears of a walk the member cap stopped. */
export function readTar(
  buffer: ArrayBuffer,
  onCapped?: () => void,
): Map<string, Uint8Array<ArrayBuffer>> {
  const tar = new Uint8Array(buffer);
  const found = new Map<string, Uint8Array<ArrayBuffer>>();
  let at = 0;
  let longName: string | null = null;
  for (let seen = 0; at + BLOCK <= tar.length; seen += 1) {
    const block = tar.subarray(at, at + BLOCK);
    if (block.every((byte) => byte === 0)) break;
    if (seen >= MAX_MEMBERS) {
      onCapped?.();
      break;
    }
    vetted(block);
    const size = octal(block, 124, 12);
    const start = at + BLOCK;
    if (start + size > tar.length) throw new Error("not a tar");
    const body = tar.subarray(start, start + size);
    at = start + Math.ceil(size / BLOCK) * BLOCK;

    const type = String.fromCharCode(block[156]);
    if (type === "x") {
      longName = paxPath(body);
    } else if (type === "L") {
      longName = field(body, 0, body.length);
    } else if (type !== "g") {
      const name = memberName(block, longName);
      longName = null;
      if (isRegular(type) && safe(name)) found.set(name, body.slice());
    }
  }
  return stripRoot(found);
}

/** A member's name: the long-name record when one preceded it, else prefix and name fields. */
function memberName(block: Uint8Array, longName: string | null): string {
  const prefix = field(block, 345, 155);
  const stored = prefix ? `${prefix}/${field(block, 0, 100)}` : field(block, 0, 100);
  return (longName ?? stored).replace(/^\.\//, "");
}

function isRegular(type: string): boolean {
  return type === "0" || type === "\0" || type === "7";
}

/** Publish wraps the project in one folder: when every name shares that root, drop it. */
function stripRoot(
  found: Map<string, Uint8Array<ArrayBuffer>>,
): Map<string, Uint8Array<ArrayBuffer>> {
  const names = [...found.keys()];
  const roots = new Set(names.map((name) => name.split("/", 1)[0]));
  if (roots.size !== 1 || !names.every((name) => name.includes("/"))) return found;
  const strip = names[0].indexOf("/") + 1;
  return new Map([...found].map(([name, body]) => [name.slice(strip), body]));
}

function isPng(bytes: Uint8Array): boolean {
  return PNG_SIGNATURE.every((byte, i) => bytes[i] === byte);
}

/** A change's pieces, capped. What a cap or the PNG check left out is a sentence in `notes`. */
export function parseChange(buffer: ArrayBuffer): ChangeArchive {
  const notes: string[] = [];
  const members = readTar(buffer, () =>
    notes.push(`archive members past the first ${MAX_MEMBERS} are not shown`),
  );

  const text = (name: string): string | null => {
    const body = members.get(name);
    if (!body) return null;
    if (body.length > MAX_TEXT_BYTES) notes.push(`${name}: the first 1 MiB is shown`);
    return decoder.decode(body.subarray(0, MAX_TEXT_BYTES));
  };

  const names = [...members.keys()].filter((name) => name.startsWith(FOLDER)).sort();
  const shown = names.slice(0, MAX_SCREENSHOTS).flatMap((name) => {
    const bytes = members.get(name);
    return bytes && bytes.length <= MAX_SCREENSHOT_BYTES && isPng(bytes) ? [{ name, bytes }] : [];
  });
  if (shown.length < names.length) {
    notes.push(
      `${names.length - shown.length} screenshot(s) not shown: over the cap, or not a PNG`,
    );
  }

  const change = {
    report: text("report.md"),
    diff: text("patch.diff"),
    testOutput: text("test-output.txt"),
    screenshots: shown,
  };
  return { ...change, notes };
}
