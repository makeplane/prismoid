/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// Multimodal capture helpers for the composer - files, screenshots, camera frames, and voice→text.
// Everything a user captures becomes a task `input` (base64 of the bytes); voice is transcribed in the
// browser (Web Speech API) straight into the textarea, so it needs no backend support.

import { useEffect, useRef, useState } from "react";
import { admitFiles, MAX_INPUT_BYTES, MAX_INPUT_LABEL, overLimitMessage } from "~/lib/input-limits";
import { uuidV4 } from "~/lib/uuid";
import {
  appendHeard,
  editOverHeard,
  readResults,
  type SpeechResults,
  voiceErrorMessage,
} from "./voice";

export type Attachment = {
  id: string;
  filename: string;
  media_type: string;
  size: number;
  content: string; // base64, no data-URL prefix
  previewUrl?: string; // object URL, images only
  state: "ready" | "error";
  error?: string;
};

/** Ready attachments → the `inputs` shape a task create/follow-up expects. */
export function toInputs(attachments: Attachment[]) {
  return attachments
    .filter((a) => a.state === "ready")
    .map((a) => ({
      filename: a.filename,
      media_type: a.media_type,
      content: a.content,
    }));
}

/** What to tell the person when a ready attachment would be the one past the server's count
 *  (`held` are already on the message); its preview is freed, since it will never be shown.
 *  Undefined when it fits, or is an error chip (those carry their own message and take no slot). */
export function refuseOverLimit(att: Attachment, held: number): string | undefined {
  if (att.state !== "ready" || admitFiles(held, [att]).over.length === 0) return undefined;
  if (att.previewUrl) URL.revokeObjectURL(att.previewUrl);
  return overLimitMessage([att.filename]);
}

function blobToBase64(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const s = String(reader.result);
      resolve(s.slice(s.indexOf(",") + 1)); // drop "data:<type>;base64,"
    };
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(blob);
  });
}

async function toAttachment(blob: Blob, filename: string): Promise<Attachment> {
  const id = uuidV4();
  const media_type = blob.type || "application/octet-stream";
  const base = { id, filename, media_type, size: blob.size };
  if (blob.size > MAX_INPUT_BYTES) {
    return {
      ...base,
      content: "",
      state: "error",
      error: `${filename} is over the ${MAX_INPUT_LABEL} limit for one file.`,
    };
  }
  const content = await blobToBase64(blob);
  const previewUrl = media_type.startsWith("image/") ? URL.createObjectURL(blob) : undefined;
  return { ...base, content, previewUrl, state: "ready" };
}

export function fileToAttachment(file: File): Promise<Attachment> {
  return toAttachment(file, file.name);
}

/** Capture one frame of a chosen screen/window via the screen-share picker, as a PNG attachment. */
export async function captureScreenshot(): Promise<Attachment> {
  const media = navigator.mediaDevices as MediaDevices & {
    getDisplayMedia?: (c: MediaStreamConstraints) => Promise<MediaStream>;
  };
  if (!media?.getDisplayMedia) throw new Error("Screen capture isn't available in this browser.");
  const stream = await media.getDisplayMedia({ video: true });
  try {
    return await grabFrame(stream, `screenshot-${stamp()}.png`);
  } finally {
    stream.getTracks().forEach((t) => t.stop());
  }
}

/** Draw the current frame of a live stream to a canvas and return it as a PNG attachment. */
export async function grabFrame(stream: MediaStream, filename: string): Promise<Attachment> {
  const video = document.createElement("video");
  video.srcObject = stream;
  video.muted = true;
  await video.play();
  await new Promise((r) => setTimeout(r, 150)); // let a frame land
  const canvas = document.createElement("canvas");
  canvas.width = video.videoWidth || 1280;
  canvas.height = video.videoHeight || 720;
  canvas.getContext("2d")?.drawImage(video, 0, 0, canvas.width, canvas.height);
  video.pause();
  const blob = await new Promise<Blob | null>((r) => canvas.toBlob(r, "image/png"));
  if (!blob) throw new Error("Couldn't capture the frame.");
  return toAttachment(blob, filename);
}

function stamp(): string {
  return new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
}

// ── Voice → text (Web Speech API) ────────────────────────────────────────────────────────────────
type SpeechRec = {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  start: () => void;
  stop: () => void;
  onresult: ((e: { resultIndex: number; results: SpeechResults }) => void) | null;
  onend: (() => void) | null;
  onerror: ((e?: { error?: string }) => void) | null;
};

/** Dictation into the box. Settled words go to `onText`; the words still being heard are
 *  `hearing`, which the composer shows at the end of the box as they are spoken (waiting for a
 *  pause before any text appeared read as "not working"); `adopt` hands them to the person. A recogniser error is never silent:
 *  `onError` gets a sentence naming the cause (a blocked microphone, an unreachable speech
 *  service). */
export function useVoiceDictation(onText: (text: string) => void, onError?: (msg: string) => void) {
  const [recording, setRecording] = useState(false);
  const [supported, setSupported] = useState(false);
  const [hearing, setHearing] = useState("");
  const recRef = useRef<SpeechRec | null>(null);
  const adoptedRef = useRef(false);
  const onTextRef = useRef(onText);
  onTextRef.current = onText;
  const onErrorRef = useRef(onError);
  onErrorRef.current = onError;

  useEffect(() => {
    const w = window as unknown as {
      SpeechRecognition?: new () => SpeechRec;
      webkitSpeechRecognition?: new () => SpeechRec;
    };
    const Ctor = w.SpeechRecognition ?? w.webkitSpeechRecognition;
    if (!Ctor) return;
    setSupported(true);
    const rec = new Ctor();
    rec.continuous = true;
    rec.interimResults = true;
    rec.lang = navigator.language || "en-US";
    rec.onresult = (e) => {
      const { settled, hearing: heard } = readResults(e.resultIndex, e.results);
      if (adoptedRef.current) {
        // The words of this utterance are already in the box as the person's own text: drop
        // them until the recogniser settles it, then listen normally again.
        if (settled) adoptedRef.current = false;
        setHearing("");
        return;
      }
      if (settled) onTextRef.current(settled);
      setHearing(heard);
    };
    const end = () => {
      setRecording(false);
      setHearing("");
    };
    rec.onend = end;
    rec.onerror = (e) => {
      end();
      const msg = voiceErrorMessage(e?.error);
      if (msg) onErrorRef.current?.(msg);
    };
    recRef.current = rec;
    return () => {
      try {
        rec.stop();
      } catch {
        /* ignore */
      }
    };
  }, []);

  function toggle() {
    const rec = recRef.current;
    if (!rec) return;
    if (recording) {
      rec.stop();
      setRecording(false);
      setHearing("");
    } else {
      try {
        adoptedRef.current = false;
        rec.start();
        setRecording(true);
      } catch {
        /* already started */
      }
    }
  }

  /** The words being heard were taken into the box as the person's own text (they edited over
   *  them, or sent): the recogniser's own delivery of that utterance is dropped. */
  function adopt() {
    if (!hearing) return;
    adoptedRef.current = true;
    setHearing("");
  }

  return { recording, supported, hearing, toggle, adopt };
}

/** The box's text while dictation is on. Words still being heard show in the box itself, at its
 *  end, as they are spoken; they are not the person's text until the recogniser settles them,
 *  or until the person takes them: by editing over them (`edit`) or by sending (`keep`, so a
 *  failed send keeps them in the draft and they are never delivered twice). */
export function useHeardText(
  text: string,
  setText: (text: string) => void,
  voice: { hearing: string; adopt: () => void },
) {
  const shown = appendHeard(text, voice.hearing);
  return {
    shown,
    edit(value: string) {
      const next = editOverHeard(value, voice.hearing);
      if (next.adopted) voice.adopt();
      setText(next.text);
    },
    keep() {
      if (!voice.hearing) return;
      setText(shown);
      voice.adopt();
    },
  };
}
