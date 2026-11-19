/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The screen's wire: one framing in both directions, bounded, and `n` agreeing with
// the payload. A refusal here is what keeps a hostile or broken server message from reaching a
// canvas (the server holds the other half; this is the browser's).
import { describe, expect, it } from "vitest";
import { decode, encode, HEADER_MAX, MESSAGE_MAX, WireError } from "./screen-wire";

/** A raw message: an explicit length prefix, then whatever bytes the test wants after it. */
function raw(prefix: number, body: Uint8Array | string): ArrayBuffer {
  const bytes = typeof body === "string" ? new TextEncoder().encode(body) : body;
  const out = new Uint8Array(4 + bytes.byteLength);
  new DataView(out.buffer).setUint32(0, prefix, false);
  out.set(bytes, 4);
  return out.buffer;
}

const buffer = (u: Uint8Array): ArrayBuffer =>
  u.buffer.slice(u.byteOffset, u.byteOffset + u.byteLength) as ArrayBuffer;

describe("encode", () => {
  it("writes a big-endian length, the compact JSON header with n, then the payload", () => {
    const out = encode({ t: "hello", v: 1 });
    const json = '{"t":"hello","v":1,"n":0}';
    expect(new DataView(out.buffer).getUint32(0, false)).toBe(json.length);
    expect(new TextDecoder().decode(out.subarray(4))).toBe(json);
  });

  it("sets n from the payload, overwriting a caller's n", () => {
    const out = encode({ t: "frame", n: 99 }, new Uint8Array([1, 2, 3]));
    const { header, payload } = decode(buffer(out));
    expect(header.n).toBe(3);
    expect(Array.from(payload)).toEqual([1, 2, 3]);
  });

  it("refuses a header over 4096 bytes and a message over 4 MiB", () => {
    expect(() => encode({ t: "text", text: "x".repeat(HEADER_MAX) })).toThrow(WireError);
    expect(() => encode({ t: "frame" }, new Uint8Array(MESSAGE_MAX))).toThrow(WireError);
  });
});

describe("decode", () => {
  it("round-trips a frame: the header's fields and the JPEG bytes", () => {
    const jpeg = new Uint8Array([0xff, 0xd8, 0xff, 0xe0, 0, 0x10]);
    const msg = encode({ t: "frame", seq: 7, ts: 1.5, w: 1280, h: 900, fmt: "jpeg" }, jpeg);
    const { header, payload } = decode(buffer(msg));
    expect(header).toEqual({ t: "frame", seq: 7, ts: 1.5, w: 1280, h: 900, fmt: "jpeg", n: 6 });
    expect(Array.from(payload)).toEqual(Array.from(jpeg));
  });

  it("reads a header without n as a message without a payload", () => {
    expect(decode(raw(11, '{"t":"bye"}')).header).toEqual({ t: "bye" });
  });

  it("refuses a message shorter than its prefix, or whose header runs past its end", () => {
    expect(() => decode(new Uint8Array([0, 0]).buffer)).toThrow(/shorter than its length prefix/);
    expect(() => decode(raw(50, '{"t":"x"}'))).toThrow(/runs past the end/);
  });

  it("refuses a header over 4096 bytes before reading it", () => {
    expect(() => decode(raw(HEADER_MAX + 1, "{}"))).toThrow(/limit is 4096/);
  });

  it("refuses a message over 4 MiB", () => {
    expect(() => decode(new ArrayBuffer(MESSAGE_MAX + 1))).toThrow(/limit is/);
  });

  it("refuses a header that is not JSON, not an object, or has no type", () => {
    expect(() => decode(raw(3, "{{{"))).toThrow(/not JSON/);
    expect(() => decode(raw(2, "[]"))).toThrow(/no type/);
    expect(() => decode(raw(8, '{"t":42}'))).toThrow(/no type/);
    expect(() => decode(raw(4, new Uint8Array([0x7b, 0xff, 0xfe, 0x7d])))).toThrow(/not JSON/);
  });

  it("refuses an n that disagrees with the payload", () => {
    const head = '{"t":"frame","n":3}';
    const body = new Uint8Array(head.length + 2);
    body.set(new TextEncoder().encode(head));
    expect(() => decode(raw(head.length, body))).toThrow(
      /says 3 payload bytes; the message carries 2/,
    );
  });
});
