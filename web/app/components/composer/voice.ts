/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The decisions behind dictation, pure: what a recogniser result holds, what its error means
// to the person, and how a frame of microphone spectrum becomes the meter's bars. The hook
// (capture.ts) and the meter (voice-meter.tsx) only wire these to the browser.

export type SpeechResults = ArrayLike<ArrayLike<{ transcript: string }> & { isFinal?: boolean }>;

/** Split one result event into the words that are settled and the words still being heard.
 *  A result with no `isFinal` flag is settled (the flag only exists with interim results on). */
export function readResults(resultIndex: number, results: SpeechResults) {
  let settled = "";
  let hearing = "";
  for (let i = resultIndex; i < results.length; i++) {
    const text = results[i][0].transcript;
    if (results[i].isFinal === false) hearing += text;
    else settled += text;
  }
  return { settled: settled.trim(), hearing: hearing.trim() };
}

/** The box's text with heard words added at its end, one space between: the same join for words
 *  still being heard (shown live) and for words that have settled (kept). */
export function appendHeard(text: string, heard: string): string {
  if (!heard) return text;
  return (text.trim() ? text.trimEnd() + " " : "") + heard;
}

/** An edit made while heard words are showing at the end of the box. Typing ahead of them
 *  leaves them the recogniser's (`adopted` false): the person's text is the value minus those
 *  words, which the recogniser will hand over, or revise, itself. Any edit that touched them (a
 *  character after them, a backspace into them) makes the whole value the person's own, and the
 *  recogniser must not deliver those words a second time (`adopted` true). */
export function editOverHeard(value: string, heard: string): { text: string; adopted: boolean } {
  if (!heard) return { text: value, adopted: false };
  if (value.endsWith(heard)) return { text: value.slice(0, -heard.length), adopted: false };
  return { text: value, adopted: true };
}

/** What to tell the person when the recogniser stops with an error; null when there is nothing
 *  to say (they stopped it, or simply did not speak). */
export function voiceErrorMessage(code: string | undefined): string | null {
  switch (code) {
    case "aborted":
    case "no-speech":
      return null;
    case "not-allowed":
    case "service-not-allowed":
      return "Microphone access is blocked. Allow it for this site in your browser, then try again.";
    case "audio-capture":
      return "No microphone was found.";
    case "network":
      return "Dictation could not reach the browser's speech service. Check your connection, or try Chrome or Safari.";
    case "language-not-supported":
      return "Dictation does not support your browser's language.";
    default:
      return "Dictation stopped unexpectedly. Try again.";
  }
}

/** The quietest a bar draws: a still row of dots, never an empty gap. */
export const BAR_FLOOR = 0.12;

/** Fold a frame of byte spectrum (0-255 per bin) into `count` bar heights, 0-1. Speech lives in
 *  the lower bins, so only the first two thirds are read; each bar is its band's mean. */
export function barLevels(spectrum: ArrayLike<number>, count: number): number[] {
  const usable = Math.max(count, Math.floor((spectrum.length * 2) / 3));
  const band = usable / count;
  return Array.from({ length: count }, (_, i) => {
    const from = Math.floor(i * band);
    const to = Math.max(from + 1, Math.floor((i + 1) * band));
    let sum = 0;
    for (let b = from; b < to; b++) sum += spectrum[b] ?? 0;
    return Math.max(BAR_FLOOR, Math.min(1, sum / (to - from) / 180));
  });
}
