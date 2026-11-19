/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import ReactMarkdown, { type Components, type ExtraProps } from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  blockLineOffsets,
  endsInOpenFence,
  extractLinkDefs,
  healTail,
  splitBlocks,
} from "~/lib/markdown-stream";
import { imageAllowed } from "~/lib/image-policy";
import { remarkBreaks } from "~/lib/remark-breaks";
import { cn } from "~/lib/utils";
import { readFence, scanFences } from "./fence";
import { SPECIAL_BLOCKS } from "./special-blocks";
import { ImageIcon } from "lucide-react";
import { lazy, memo, Suspense, useMemo, type JSX, type ReactNode } from "react";

// Standard GFM, and the chat variant: `breaks` turns a newline in a paragraph into a line break,
// so a poem in an answer keeps its rows. Only conversation surfaces ask for it; a document
// (an artifact preview, a bot's instructions) stays CommonMark, where a hard-wrapped file reflows.
const remarkPlugins = [remarkGfm];
const chatRemarkPlugins = [remarkGfm, remarkBreaks];

// Shiki rides ONLY this lazy chunk: an eager import would ship the highlighter
// core to every route through the kit chunk. The Suspense fallback is the CSS-styled <pre>
// the app always had - degradation is legible, and it is what static renders (tests) pin.
const LazyAnswerCodeBlock = lazy(() => import("./answer-code-block"));

// External links leave the SPA deliberately and safely; everything else (hash, relative,
// mailto) keeps the browser's default. v10's urlTransform has already dropped javascript:
// and data: schemes before this sees them.
function anchor({ node, children, href, ...props }: JSX.IntrinsicElements["a"] & ExtraProps) {
  void node;
  const external = typeof href === "string" && /^(https?:)?\/\//i.test(href);
  return (
    <a
      href={href}
      {...(external ? { target: "_blank", rel: "noopener noreferrer" } : {})}
      {...props}
    >
      {children}
    </a>
  );
}

// A model-authored image the policy will not auto-load (`lib/image-policy.imageAllowed`):
// a chip that opens on click, the reader's choice. Its click stays its own, so a chip inside
// a clickable card (a gallery tile) opens the image without selecting the card.
export function ImageChip({ src, alt }: { src?: string; alt?: string }) {
  const name =
    (alt && alt.trim()) || (src ? (src.split("/").pop()?.split("?")[0] ?? "image") : "image");
  return (
    <a
      href={src}
      target="_blank"
      rel="noopener noreferrer"
      onClick={(e) => e.stopPropagation()}
      className="bg-muted text-muted-foreground hover:text-foreground inline-flex max-w-full items-center gap-1 rounded-sm px-1.5 py-0.5 align-[-0.125em] text-caption-md-regular no-underline"
    >
      <ImageIcon className="size-3.5 shrink-0" />
      <span className="truncate">{name}</span>
    </a>
  );
}

function image({ node, src, alt, ...props }: JSX.IntrinsicElements["img"] & ExtraProps) {
  void node;
  if (typeof src === "string" && src !== "" && imageAllowed(src)) {
    return <img src={src} alt={alt ?? ""} loading="lazy" {...props} />;
  }
  return <ImageChip src={typeof src === "string" ? src : undefined} alt={alt} />;
}

// `inert`: the text is an agent's, read on the user's own machine, so a path in it must never
// become something a click opens (or a page loads). Only a web link stays a link; any other
// link is its words, an image is its alt text, and no special block mounts.
function inertAnchor({ node, children, href, ...props }: JSX.IntrinsicElements["a"] & ExtraProps) {
  if (typeof href === "string" && /^https?:\/\//i.test(href)) {
    return anchor({ node, children, href, ...props });
  }
  return <span>{children}</span>;
}

function inertImage({ alt }: JSX.IntrinsicElements["img"] & ExtraProps) {
  return <span className="text-muted-foreground">{alt || "image"}</span>;
}

// A wide table (the chair tabulates) must scroll inside its own box - otherwise it widens
// the page and hands the window a second, horizontal scrollbar that shifts mid-stream. The
// frame (ring, radius, scroll) is app.css's `.markdown .table-frame`.
function table({ node, ...props }: JSX.IntrinsicElements["table"] & ExtraProps) {
  void node;
  return (
    <div className="table-frame">
      <table {...props} />
    </div>
  );
}

// The body of a special block renders through THIS set: same table/link/image conduct, but
// no fence dispatch - a fence quoted inside an email body stays a plain code block, and no
// block can recurse into the registry (the nesting rule, held structurally).
const plainComponents: Components = { a: anchor, img: image, table };

function renderPlainBody(md: string): ReactNode {
  return (
    <ReactMarkdown remarkPlugins={remarkPlugins} components={plainComponents}>
      {md}
    </ReactMarkdown>
  );
}

/** One fence's render facts, resolved against the FULL source and handed to its chunk in
 *  chunk-local line coordinates - a chunk knows nothing of its siblings except through
 *  these (ordinal/total keep "Draft 2 of 3" honest across chunk boundaries). */
interface ChunkFence {
  localLine: number;
  closed: boolean;
  ordinal: number;
  total: number;
}

function buildComponents(
  fences: Map<number, ChunkFence>,
  live: boolean,
  inert: boolean,
): Components {
  return {
    a: inert ? inertAnchor : anchor,
    img: inert ? inertImage : image,
    table,
    // The dispatch point. v10 hands the hast node to function components only;
    // the code child is already rendered by the time `pre` sees React children, so language
    // and raw source come off the tree (kit/fence.ts), never out of the children.
    pre({ node, children, ...props }: JSX.IntrinsicElements["pre"] & ExtraProps) {
      const fence = readFence(node);
      if (!fence) return <pre {...props}>{children}</pre>;

      const line = node?.position?.start.line;
      const entry = line === undefined ? undefined : fences.get(line);
      // No entry = a fence the scan could not track (blockquote-nested, 4+-space list
      // depth) - while LIVE it must be presumed OPEN: actions on half a draft (or mermaid
      // re-rendering partial source per delta) is worse than a copy button arriving at
      // settle. Indented code blocks land here too (never scanned; readFence gives them
      // lang "") and share the conservative treatment: the live tail's code hides copy
      // until the segment settles - which is when `live` flips false and this clears.
      const streaming = live && (entry === undefined || !entry.closed);

      const Special = inert ? undefined : SPECIAL_BLOCKS[fence.lang];
      if (Special) {
        return (
          <Special
            raw={fence.raw}
            streaming={streaming}
            ordinal={entry?.ordinal ?? 0}
            total={entry?.total ?? 1}
            renderBody={renderPlainBody}
          />
        );
      }

      return (
        <Suspense fallback={<pre {...props}>{children}</pre>}>
          <LazyAnswerCodeBlock code={fence.raw} language={fence.lang} streaming={streaming} />
        </Suspense>
      );
    },
  };
}

interface ChunkSpec {
  breaks: boolean;
  source: string;
  fences: ChunkFence[];
  live: boolean;
  inert: boolean;
}

function sameFences(a: ChunkFence[], b: ChunkFence[]): boolean {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) {
    const x = a[i];
    const y = b[i];
    if (
      x.localLine !== y.localLine ||
      x.closed !== y.closed ||
      x.ordinal !== y.ordinal ||
      x.total !== y.total
    ) {
      return false;
    }
  }
  return true;
}

// One parsed block. While the segment streams, every settled chunk memo-skips and only the
// tail re-parses per frame - the whole-doc O(n^2) reparse the audit measured is gone.
const Chunk = memo(
  function Chunk({ source, fences, live, inert, breaks }: ChunkSpec) {
    const components = useMemo(
      () => buildComponents(new Map(fences.map((f) => [f.localLine, f])), live, inert),
      [fences, live, inert],
    );
    return (
      <ReactMarkdown
        remarkPlugins={breaks ? chatRemarkPlugins : remarkPlugins}
        components={components}
      >
        {source}
      </ReactMarkdown>
    );
  },
  (prev, next) =>
    prev.source === next.source &&
    prev.live === next.live &&
    prev.inert === next.inert &&
    prev.breaks === next.breaks &&
    sameFences(prev.fences, next.fences),
);

function planChunks(source: string, live: boolean): Omit<ChunkSpec, "inert" | "breaks">[] {
  const scan = scanFences(source);
  const countByLang = new Map<string, number>();
  const ordinalByLine = new Map<number, number>();
  for (const e of scan) {
    const n = countByLang.get(e.lang) ?? 0;
    ordinalByLine.set(e.line, n);
    countByLang.set(e.lang, n + 1);
  }

  const blocks = live ? splitBlocks(source) : [source];
  const offsets = blockLineOffsets(blocks);

  // Reference-style links: per-block LIVE parsing severs '[x][1]' from a '[1]: url' living
  // in another block, so the definitions are collected once from the full source (outside
  // fences) and appended to every chunk's parse input. Definitions parse to nothing
  // visible (duplicates ignored - CommonMark keeps the first), refs resolve, and the hast
  // positions the fence map keys on sit above the appendix, untouched. A definition
  // arriving late re-sources every settled chunk for one frame; the memo re-settles after.
  // Footnotes are deliberately NOT carried this way: a '[^1]: note' definition RENDERS in
  // place under gfm, so appending it would duplicate the note - footnote refs stay
  // settle-only. Nothing is appended to a chunk still inside an open fence: there the
  // appendix would become visible code (a draft body growing a stray '[1]: url' line).
  const defs = live ? extractLinkDefs(source) : [];

  return blocks.map((block, i) => {
    const start = offsets[i];
    const end = start + block.split("\n").length - 1;
    const isTail = live && i === blocks.length - 1;
    const fences = scan
      .filter((e) => e.line >= start && e.line <= end)
      .map((e) => ({
        localLine: e.line - start + 1,
        closed: e.closed,
        ordinal: ordinalByLine.get(e.line) ?? 0,
        total: countByLang.get(e.lang) ?? 1,
      }));
    const src = isTail ? healTail(block) : block;
    const withDefs =
      defs.length > 0 && src !== "" && !endsInOpenFence(src) ? `${src}\n\n${defs.join("\n")}` : src;
    return { source: withDefs, fences, live: isTail };
  });
}

/** Rendered markdown, token-styled via the `.markdown` layer in app.css. Used for streamed
 *  assistant text and artifact previews. Streamed text is append-only markdown (never HTML;
 *  v10 drops raw HTML by design). `live` marks a still-streaming segment: the source splits
 *  into memoized blocks (only the tail re-parses per delta), the tail's half-typed syntax
 *  is healed, and a fence open at the tail renders its streaming variant (plain code body,
 *  block actions hidden). The chunks are Fragments under the ONE .markdown div, so margins
 *  and the stream-tail caret behave exactly as a single parse. */
export function Markdown({
  children,
  className,
  live = false,
  inert = false,
  breaks = false,
}: {
  children: string;
  className?: string;
  live?: boolean;
  /** Text from an agent working on the user's own files: no path becomes a link (see `inertAnchor`). */
  inert?: boolean;
  /** Chat surfaces: a newline inside a paragraph is a line break (a poem keeps its rows). */
  breaks?: boolean;
}) {
  const chunks = useMemo(() => planChunks(children, live), [children, live]);
  return (
    <div className={cn("markdown", className)}>
      {chunks.map((chunk, i) => (
        <Chunk key={i} {...chunk} inert={inert} breaks={breaks} />
      ))}
    </div>
  );
}
