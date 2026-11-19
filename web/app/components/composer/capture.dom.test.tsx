/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// Composer capture: files become ready or too-large attachments, only ready ones become task
// inputs, a missing screen-capture API is a plain error, and voice dictation toggles a
// recogniser and hands its transcript to the box.
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MAX_INPUT_BYTES } from "~/lib/input-limits";
import {
  captureScreenshot,
  fileToAttachment,
  toInputs,
  useVoiceDictation,
  type Attachment,
} from "./capture";

beforeEach(() => {
  URL.createObjectURL = vi.fn(() => "blob:preview");
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  delete (window as unknown as Record<string, unknown>).webkitSpeechRecognition;
});

describe("fileToAttachment", () => {
  it("reads a text file to base64 with its name and type", async () => {
    const a = await fileToAttachment(new File(["hi"], "note.txt", { type: "text/plain" }));
    expect(a).toMatchObject({
      filename: "note.txt",
      media_type: "text/plain",
      size: 2,
      content: btoa("hi"),
      state: "ready",
    });
    expect(a.previewUrl).toBeUndefined();
  });

  it("gives an image a preview url and an untyped file the octet-stream type", async () => {
    const img = await fileToAttachment(new File(["x"], "p.png", { type: "image/png" }));
    expect(img.previewUrl).toBe("blob:preview");
    const raw = await fileToAttachment(new File(["x"], "blob"));
    expect(raw.media_type).toBe("application/octet-stream");
  });

  it("refuses a file over the cap without reading it", async () => {
    const big = new File(["x"], "big.bin");
    Object.defineProperty(big, "size", { value: MAX_INPUT_BYTES + 1 });
    const a = await fileToAttachment(big);
    expect(a.state).toBe("error");
    expect(a.error).toBe("big.bin is over the 5 MB limit for one file.");
    expect(a.content).toBe("");
  });
});

describe("toInputs", () => {
  it("keeps only ready attachments, with just the wire fields", () => {
    const ready: Attachment = {
      id: "1",
      filename: "a.txt",
      media_type: "text/plain",
      size: 1,
      content: "QQ==",
      state: "ready",
    };
    const bad: Attachment = { ...ready, id: "2", state: "error", error: "no" };
    expect(toInputs([ready, bad])).toEqual([
      { filename: "a.txt", media_type: "text/plain", content: "QQ==" },
    ]);
  });
});

describe("captureScreenshot", () => {
  it("says so when the browser cannot share a screen", async () => {
    vi.stubGlobal("navigator", { mediaDevices: {} });
    await expect(captureScreenshot()).rejects.toThrow(/isn't available/);
  });
});

type Rec = {
  start: ReturnType<typeof vi.fn>;
  stop: ReturnType<typeof vi.fn>;
  onresult: ((e: unknown) => void) | null;
  onend: (() => void) | null;
  onerror: ((e?: { error?: string }) => void) | null;
};

function stubSpeech() {
  const made: Rec[] = [];
  class Fake {
    start = vi.fn();
    stop = vi.fn();
    onresult: Rec["onresult"] = null;
    onend: Rec["onend"] = null;
    onerror: Rec["onerror"] = null;
    constructor() {
      made.push(this as unknown as Rec);
    }
  }
  (window as unknown as Record<string, unknown>).webkitSpeechRecognition = Fake;
  return made;
}

describe("useVoiceDictation", () => {
  it("is unsupported without a speech API, and toggle does nothing", () => {
    const { result } = renderHook(() => useVoiceDictation(vi.fn()));
    expect(result.current.supported).toBe(false);
    act(() => result.current.toggle());
    expect(result.current.recording).toBe(false);
  });

  it("starts and stops the recogniser and passes the trimmed transcript on", () => {
    const made = stubSpeech();
    const onText = vi.fn();
    const { result } = renderHook(() => useVoiceDictation(onText));
    expect(result.current.supported).toBe(true);
    act(() => result.current.toggle());
    expect(made[0].start).toHaveBeenCalled();
    expect(result.current.recording).toBe(true);
    act(() => made[0].onresult?.({ resultIndex: 0, results: [[{ transcript: "  buy milk " }]] }));
    expect(onText).toHaveBeenCalledWith("buy milk");
    act(() => result.current.toggle());
    expect(made[0].stop).toHaveBeenCalled();
    expect(result.current.recording).toBe(false);
  });

  it("recording ends when the recogniser ends on its own", () => {
    const made = stubSpeech();
    const { result } = renderHook(() => useVoiceDictation(vi.fn()));
    act(() => result.current.toggle());
    act(() => made[0].onend?.());
    expect(result.current.recording).toBe(false);
  });

  it("shows the words still being heard, then hands over the settled ones", () => {
    const made = stubSpeech();
    const onText = vi.fn();
    const { result } = renderHook(() => useVoiceDictation(onText));
    act(() => result.current.toggle());
    const hearing = Object.assign([{ transcript: "buy mi" }], { isFinal: false });
    act(() => made[0].onresult?.({ resultIndex: 0, results: [hearing] }));
    expect(result.current.hearing).toBe("buy mi");
    expect(onText).not.toHaveBeenCalled();
    const settled = Object.assign([{ transcript: "buy milk" }], { isFinal: true });
    act(() => made[0].onresult?.({ resultIndex: 0, results: [settled] }));
    expect(onText).toHaveBeenCalledWith("buy milk");
    expect(result.current.hearing).toBe("");
  });

  it("adopted words are never handed over a second time; the next utterance is heard normally", () => {
    const made = stubSpeech();
    const onText = vi.fn();
    const { result } = renderHook(() => useVoiceDictation(onText));
    act(() => result.current.toggle());
    const heard = (t: string, isFinal: boolean) => ({
      resultIndex: 0,
      results: [Object.assign([{ transcript: t }], { isFinal })],
    });
    act(() => made[0].onresult?.(heard("buy mi", false)));
    act(() => result.current.adopt());
    expect(result.current.hearing).toBe("");
    act(() => made[0].onresult?.(heard("buy milk", false)));
    expect(result.current.hearing).toBe("");
    act(() => made[0].onresult?.(heard("buy milk", true)));
    expect(onText).not.toHaveBeenCalled();
    act(() => made[0].onresult?.(heard("and eggs", true)));
    expect(onText).toHaveBeenCalledWith("and eggs");
  });

  it("stops and names the cause when the recogniser errors", () => {
    const made = stubSpeech();
    const onError = vi.fn();
    const { result } = renderHook(() => useVoiceDictation(vi.fn(), onError));
    act(() => result.current.toggle());
    act(() => made[0].onerror?.({ error: "not-allowed" }));
    expect(result.current.recording).toBe(false);
    expect(onError).toHaveBeenCalledWith(expect.stringMatching(/blocked/));
  });
});
