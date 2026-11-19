/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// What the server accepts as a task's attachments, and what to say when a file is over it.
// The numbers are the server's (backend/prismoid/schemas/inputs.py: MAX_INPUTS and
// MAX_INPUT_BYTES, stated in the API contract as `inputs.maxItems` and the base64 ceiling of
// `content`); `input-limits.test.ts` pins both to docs/api/openapi.json, so a server change
// fails there instead of in front of a customer.

export const MAX_INPUTS = 10;
export const MAX_INPUT_BYTES = 5 * 1024 * 1024;

/** "5 MB", the spelling every message about the size limit uses. */
export const MAX_INPUT_LABEL = `${MAX_INPUT_BYTES / (1024 * 1024)} MB`;

/** Split the files a person chose into the ones that fit the free slots and the ones that
 *  do not, in the order chosen. `held` is how many attachments are already on the message. */
export function admitFiles<T>(held: number, incoming: readonly T[]): { fit: T[]; over: T[] } {
  const room = Math.max(0, MAX_INPUTS - held);
  return { fit: incoming.slice(0, room), over: incoming.slice(room) };
}

/** Which files were left off, in plain words. */
export function overLimitMessage(names: readonly string[]): string {
  const shown = names.slice(0, 3).join(", ");
  const more = names.length > 3 ? ` and ${names.length - 3} more` : "";
  return `A message takes up to ${MAX_INPUTS} files. Not attached: ${shown}${more}.`;
}

/** The server's 422 on attachments (`fields` keyed `inputs` for the count, `inputs.N.content`
 *  for one file's size, `inputs.N.filename` for a bad name) as a sentence about the files the
 *  person attached, or undefined when the error is not about attachments. `names` are the
 *  files in the order they were sent. */
export function inputsProblem(
  fields: Readonly<Record<string, string>> | undefined,
  names: readonly string[],
): string | undefined {
  for (const [key, reason] of Object.entries(fields ?? {})) {
    if (key === "inputs")
      return `A message takes up to ${MAX_INPUTS} files. Remove some and send again.`;
    const one = /^inputs\.(\d+)\.(\w+)$/.exec(key);
    if (!one) continue;
    const name = names[Number(one[1])] ?? "A file";
    return one[2] === "content"
      ? `${name} is over the ${MAX_INPUT_LABEL} limit for one file.`
      : `${name}: ${reason}`;
  }
  return undefined;
}
