/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// ```mermaid → a rendered diagram.
// The landscape's buggiest surface, so every mitigation is structural: the library arrives
// ONLY by dynamic import inside an effect (never in an eager chunk - the collection-lazy
// precedent), only a CLOSED fence renders (a half-streamed diagram is the source, plainly),
// securityLevel stays "strict", the image policy holds (a source that would fetch while it
// renders never reaches mermaid; the output loses any fetch to another origin), any
// parse/render failure falls back to the source with a quiet note, and the static/server render is a placeholder the house tests can pin. The
// SVG injected below is the sanitizer's OUTPUT, not streamed text - the never-HTML rule
// governs the stream, and this is the same class of rendering as shiki's token spans.
import { mermaidLoadsExternal, withoutExternalImages } from "~/lib/image-policy";
import { CopyAction } from "./draft-actions";
import { SpecialBlockShell, type SpecialBlockProps } from "./special-blocks";
import { WorkflowIcon } from "lucide-react";
import { useTheme } from "next-themes";
import { useEffect, useId, useState } from "react";

// Rendered-SVG cache, module-level and small. At settle the renderer collapses its chunk
// list back into ONE chunk (planChunks returns [source]), which remounts every block -
// without this cache the settled diagram flashes back to its source and mermaid renders
// the same text twice. Keyed by source + theme; Map iteration is insertion order, so the
// cap evicts the oldest entry.
const SVG_CACHE = new Map<string, string>();
const SVG_CACHE_CAP = 50;
// mermaid's own default `secure` keys, kept: listing more replaces the list, never extends it.
const MERMAID_SECURE = [
  "secure",
  "securityLevel",
  "startOnLoad",
  "maxTextSize",
  "suppressErrorRendering",
  "maxEdges",
];

function cacheKey(raw: string, theme: string | undefined): string {
  return `${theme ?? "default"}\u0000${raw}`;
}

export function MermaidBlock({ raw, streaming }: SpecialBlockProps) {
  const { resolvedTheme } = useTheme();
  const reactId = useId();
  // A cache hit seeds state synchronously - the remount-at-settle paints the diagram on
  // its first frame, no source flash, no second mermaid.render.
  const [svg, setSvg] = useState<string | null>(
    () => SVG_CACHE.get(cacheKey(raw, resolvedTheme)) ?? null,
  );
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (streaming) return;
    const key = cacheKey(raw, resolvedTheme);
    const hit = SVG_CACHE.get(key);
    if (hit !== undefined) {
      // Covers theme flips back to an already-rendered look, not just the mount seed.
      setFailed(false);
      setSvg(hit);
      return;
    }
    let cancelled = false;
    setFailed(false);
    void (async () => {
      try {
        if (mermaidLoadsExternal(raw)) throw new Error("the diagram would load an image");
        const { default: mermaid } = await import("mermaid");
        mermaid.initialize({
          startOnLoad: false,
          securityLevel: "strict",
          // what a diagram's own %%{init}%% may not change: its CSS and the sanitizer level
          secure: [...MERMAID_SECURE, "themeCSS", "themeVariables", "fontFamily", "altFontFamily"],
          // mermaid 11 otherwise appends its own error diagram to document.body before
          // throwing; the catch below already owns failure (the source card).
          suppressErrorRendering: true,
          theme: resolvedTheme === "dark" ? "dark" : "default",
          fontFamily: "var(--font-sans)",
        });
        const id = `mmd-${reactId.replace(/[^a-zA-Z0-9]/g, "")}`;
        const out = await mermaid.render(id, raw);
        const clean = withoutExternalImages(out.svg);
        if (clean === null)
          throw new Error("the diagram holds markup the image policy cannot read");
        // Cache even when this instance unmounted mid-render: the settle remount is
        // exactly the consumer that wants the finished work.
        SVG_CACHE.set(key, clean);
        if (SVG_CACHE.size > SVG_CACHE_CAP) {
          const oldest = SVG_CACHE.keys().next().value;
          if (oldest !== undefined) SVG_CACHE.delete(oldest);
        }
        if (!cancelled) setSvg(clean);
      } catch {
        if (!cancelled) {
          setSvg(null);
          setFailed(true);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [raw, streaming, resolvedTheme, reactId]);

  const showSource = streaming || failed || !svg;

  return (
    <SpecialBlockShell
      block="mermaid"
      icon={WorkflowIcon}
      title={streaming ? "Drawing diagram…" : "Diagram"}
      meta={failed && <span className="font-regular">· could not render, showing the source</span>}
      actions={
        !streaming && (
          <CopyAction onCopy={() => navigator.clipboard.writeText(raw)} label="Copy source" />
        )
      }
    >
      {showSource ? (
        <pre className="bg-muted/40 m-0 overflow-x-auto p-3 font-mono text-body-sm-regular">
          {raw}
        </pre>
      ) : (
        <div
          data-mermaid-svg
          className="flex justify-center overflow-x-auto p-3 [&_svg]:h-auto [&_svg]:max-w-full"
          // Sanitized library output (securityLevel: strict), never streamed text.
          dangerouslySetInnerHTML={{ __html: svg }}
        />
      )}
    </SpecialBlockShell>
  );
}
