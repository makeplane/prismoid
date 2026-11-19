/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The dictation meter: a row of bars that rise with the speaker's voice. Mounted only while
// dictation is on. It opens its own microphone stream for the level alone (the recogniser
// owns the words), and writes each frame straight to the bars' transforms: sixty updates a
// second through React state would re-render the composer under the person's typing.
// Transform only, never height (DESIGN.md: never transition layout). If the stream is
// refused, or motion is reduced, the bars rest at the floor.
import { useEffect, useRef } from "react";
import { cn } from "~/lib/utils";
import { BAR_FLOOR, barLevels } from "./voice";

// Few enough to sit beside the mic in the box's one-line toolbar, phone width included.
const BARS = 12;

export function VoiceMeter({ className }: { className?: string }) {
  const row = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const Ctx =
      window.AudioContext ??
      (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!Ctx || !navigator.mediaDevices?.getUserMedia) return;

    let stopped = false;
    let frame = 0;
    let stream: MediaStream | null = null;
    let audio: AudioContext | null = null;

    void navigator.mediaDevices.getUserMedia({ audio: true }).then(
      (s) => {
        if (stopped) return s.getTracks().forEach((t) => t.stop());
        stream = s;
        audio = new Ctx();
        // a context made after an awaited permission prompt can start suspended
        void audio.resume();
        const analyser = audio.createAnalyser();
        analyser.fftSize = 128;
        analyser.smoothingTimeConstant = 0.75;
        audio.createMediaStreamSource(s).connect(analyser);
        const spectrum = new Uint8Array(analyser.frequencyBinCount);
        const draw = () => {
          analyser.getByteFrequencyData(spectrum);
          const levels = barLevels(spectrum, BARS);
          row.current?.childNodes.forEach((bar, i) => {
            (bar as HTMLElement).style.transform = `scaleY(${levels[i]})`;
          });
          frame = requestAnimationFrame(draw);
        };
        draw();
      },
      () => {
        /* no stream for the meter: the bars rest; the recogniser reports its own errors */
      },
    );

    return () => {
      stopped = true;
      cancelAnimationFrame(frame);
      stream?.getTracks().forEach((t) => t.stop());
      void audio?.close();
    };
  }, []);

  return (
    <span ref={row} aria-hidden className={cn("flex h-4 shrink-0 items-center gap-0.5", className)}>
      {Array.from({ length: BARS }, (_, i) => (
        <span
          key={i}
          className="h-full w-0.5 rounded-full bg-current"
          style={{ transform: `scaleY(${BAR_FLOOR})` }}
        />
      ))}
    </span>
  );
}
