/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The screen's wire (ADR-065): a length-prefixed framing,
//   [u32 big-endian header_len][JSON header, at most 4096 bytes][payload]
// in both directions, at most 4 MiB a message - the same bytes `prismoid.schemas.screen` writes.
// Every header carries `n`, the payload's byte count (0 when there is none). On the WebSocket the
// message boundary delimits too, and `decode` refuses a message whose `n` disagrees.
// Pure: no socket, no DOM. hooks/use-screen.ts owns the connection.

export const HEADER_MAX = 4096;
export const MESSAGE_MAX = 4 * 1024 * 1024;

/** A message's header. `t` names it (down: hello · frame · reset · status · masked · role · bye · cursor · vnc; up: hello · pointer · key · text · back · hint · vnc), `n` counts the payload's bytes, the rest is per type. */
export type Header = { t: string; n?: number; [k: string]: unknown };

export class WireError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "WireError";
  }
}

const utf8 = new TextEncoder();
const strictUtf8 = new TextDecoder("utf-8", { fatal: true });

/** One whole message. `n` always comes from the payload: a caller's own `n` is overwritten. */
export function encode(
  header: Header,
  payload: Uint8Array = new Uint8Array(0),
): Uint8Array<ArrayBuffer> {
  const json = utf8.encode(JSON.stringify({ ...header, n: payload.byteLength }));
  if (json.byteLength > HEADER_MAX)
    throw new WireError(`header is ${json.byteLength} bytes; the limit is ${HEADER_MAX}`);
  const size = 4 + json.byteLength + payload.byteLength;
  if (size > MESSAGE_MAX)
    throw new WireError(`message is ${size} bytes; the limit is ${MESSAGE_MAX}`);
  const out = new Uint8Array(size);
  new DataView(out.buffer).setUint32(0, json.byteLength, false);
  out.set(json, 4);
  out.set(payload, 4 + json.byteLength);
  return out;
}

/** One whole message off the socket. The payload is a view into `buf`, never a copy. */
export function decode(buf: ArrayBuffer): {
  header: Header;
  payload: Uint8Array<ArrayBuffer>;
} {
  if (buf.byteLength > MESSAGE_MAX)
    throw new WireError(`message is ${buf.byteLength} bytes; the limit is ${MESSAGE_MAX}`);
  if (buf.byteLength < 4) throw new WireError("message is shorter than its length prefix");
  const size = new DataView(buf).getUint32(0, false);
  if (size > HEADER_MAX) throw new WireError(`header is ${size} bytes; the limit is ${HEADER_MAX}`);
  if (4 + size > buf.byteLength) throw new WireError("header runs past the end of the message");
  let header: unknown;
  try {
    header = JSON.parse(strictUtf8.decode(new Uint8Array(buf, 4, size)));
  } catch {
    throw new WireError("header is not JSON");
  }
  if (
    typeof header !== "object" ||
    header === null ||
    Array.isArray(header) ||
    typeof (header as { t?: unknown }).t !== "string"
  )
    throw new WireError("header has no type");
  const payload = new Uint8Array(buf, 4 + size);
  const n = (header as Header).n ?? 0;
  if (n !== payload.byteLength)
    throw new WireError(
      `header says ${String(n)} payload bytes; the message carries ${payload.byteLength}`,
    );
  return { header: header as Header, payload };
}
