/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { API_URL } from "~/lib/api/client";

// Auto-loading a model-authored image URL is a request the READER never chose - the
// exfiltration shape the worker's web door closes, honored client-side: own-origin, the
// API's origin, and relative paths load; anything else waits for a click. One predicate for
// every surface that draws what a model wrote (markdown, the gallery cover, mermaid).
// Every src resolves against a BASE (the page; the API origin when there is no window) so
// bare relatives ("artifacts/chart.png") land on an origin the allowlist can judge instead
// of throwing out of new URL(). Protocol-relative "//evil.com/x" resolves through the same
// door to evil.com's origin and fails the check - external stays external.
export function imageAllowed(src: string): boolean {
  try {
    const base = typeof window !== "undefined" ? window.location.href : API_URL;
    const url = new URL(src, base);
    if (url.origin === new URL(API_URL).origin) return true;
    return typeof window !== "undefined" && url.origin === window.location.origin;
  } catch {
    return false;
  }
}

// What mermaid would fetch while it lays a diagram out, before there is any output to clean:
// an image node (`img:`), an HTML label tag that loads something, a mermaid entity that opens
// a tag (`#60;`), and CSS that reaches out (`url(`, `src(`, `@import`, `image-set`, `themeCSS`).
// Any backslash refuses too: a CSS escape spells `url(` without the letters (`u\rl(`, `\75 rl(`),
// and mermaid applies a diagram's styles to the live page before any output exists to clean.
const LOADS_WHILE_RENDERING = new RegExp(
  [
    String.raw`\bimg\b`,
    String.raw`<\s*/?\s*(?:image|video|audio|source|picture|object|embed|iframe|input|link|use|svg|table|td|th|tr|body|style|track|frame|meta|base)\b`,
    String.raw`\b(?:background|poster|srcset|xlink)\b`,
    String.raw`url\s*\(`,
    String.raw`\bsrc\s*\(`,
    String.raw`\\`,
    "@import",
    "image-set",
    "themecss",
    String.raw`#(?:60|x0*3c|lt);`,
  ].join("|"),
  "i",
);

/** Would rendering this mermaid source make the reader's browser fetch something? mermaid loads
 * an image node's URL to measure it, and puts label HTML in the page, while it renders - so such
 * a source is never handed to it and the block shows the source instead. Conservative on
 * purpose: a diagram that merely says "img" reads as its source, which loses nothing. */
export function mermaidLoadsExternal(raw: string): boolean {
  return LOADS_WHILE_RENDERING.test(raw);
}

const TAG =
  /<([a-zA-Z][\w:-]*)((?:\s+[^\s=>/"']+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>"']+))?)*)\s*(\/?)>/g;
const ATTR = /(\s+)([^\s=>/"']+)(?:(\s*=\s*)("([^"]*)"|'([^']*)'|([^\s>"']+)))?/g;
const STYLE_BLOCK = /(<style\b[^>]*>)([\s\S]*?)(<\/style>)/gi;
const CSS_URL = /url\(\s*(["']?)([^"')]*)\1\s*\)/gi;
const INLINE_CSS_TARGET = /^(#[\w-]*|data:image\/)/i;
const ALWAYS_DROPPED = new Set(["srcset", "background", "poster", "data", "formaction", "action"]);
const URL_ATTRS = new Set(["src", "href", "xlink:href"]);

// The serializer escapes only these in an attribute; numeric references decode too, and any
// other named reference makes the value unreadable here, so it is treated as foreign.
function decoded(value: string): string | null {
  const named: Record<string, string> = {
    amp: "&",
    quot: '"',
    apos: "'",
    lt: "<",
    gt: ">",
    nbsp: " ",
  };
  let unreadable = false;
  const out = value.replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);?/gi, (whole, ref: string) => {
    if (ref[0] === "#") {
      const code =
        ref[1] === "x" || ref[1] === "X" ? parseInt(ref.slice(2), 16) : parseInt(ref.slice(1), 10);
      return Number.isFinite(code) ? String.fromCodePoint(code) : "";
    }
    const hit = named[ref.toLowerCase()];
    if (hit === undefined) unreadable = true;
    return hit ?? whole;
  });
  return unreadable ? null : out;
}

function inlineOrAllowed(raw: string): boolean {
  const value = decoded(raw)?.trim();
  if (value === undefined) return false;
  return (
    value === "" ||
    value.startsWith("#") ||
    /^(data:image\/|blob:)/i.test(value) ||
    imageAllowed(value)
  );
}

// Written back escaped: a decoded `<` or `"` must never close the block or the attribute.
function escaped(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// CSS the policy can read loses every foreign `url()`; CSS it cannot read plainly (an escape,
// `image-set`, which fetches bare strings, or `@import`) is dropped whole - mermaid writes none.
function cleanCss(css: string): string {
  const text = decoded(css);
  if (text === null || /\\|image-set|@import/i.test(text)) return "";
  const kept = text.replace(CSS_URL, (whole, _q: string, target: string) =>
    INLINE_CSS_TARGET.test(target.trim()) ? whole : "none",
  );
  // a `url(` the pattern could not read (odd quoting) is not guessed at: the CSS goes
  const unread = kept.replace(CSS_URL, (whole, _q: string, target: string) =>
    INLINE_CSS_TARGET.test(target.trim()) ? "" : whole,
  );
  return /url\s*\(/i.test(unread) ? "" : kept;
}

function cleanTag(tag: string, name: string): string {
  const isLink = name.toLowerCase() === "a"; // a link is the reader's click, never a load
  return tag.replace(
    ATTR,
    (
      whole,
      sp: string,
      attr: string,
      eq?: string,
      _v?: string,
      dq?: string,
      sq?: string,
      bare?: string,
    ) => {
      const key = attr.toLowerCase();
      const value = dq ?? sq ?? bare ?? "";
      if (eq === undefined) return whole;
      if (key === "style") return `${sp}style="${escaped(cleanCss(value))}"`;
      if (ALWAYS_DROPPED.has(key)) return "";
      // a presentation attribute can reach out too (`fill`, `marker-end`, `filter`: url(...))
      if (/url\s*\(/i.test(value)) return cleanCss(value) === decoded(value) ? whole : "";
      if (!URL_ATTRS.has(key) || isLink) return whole;
      return inlineOrAllowed(value) ? whole : "";
    },
  );
}

/** Rendered mermaid SVG with every fetch to another origin taken out - the second line behind
 * `mermaidLoadsExternal` - or null when it holds a tag this scan cannot read (the caller then
 * shows the source: fail closed). A `src`/`href` (an `<img>` in an HTML label, an `<image>`, a
 * `<use>`) whose target the policy refuses is dropped, attributes that only ever fetch are
 * dropped, and CSS in `style` attributes and `<style>` blocks keeps only inline `url()`s. Tags
 * are read quote-aware, so a `>` inside an attribute value cannot end one early. */
export function withoutExternalImages(svg: string): string | null {
  if ((svg.match(/<[a-zA-Z]/g) ?? []).length !== (svg.match(TAG) ?? []).length) return null;
  const tagged = svg.replace(TAG, (tag: string, name: string) => cleanTag(tag, name));
  return tagged.replace(
    STYLE_BLOCK,
    (_w, open: string, css: string, close: string) => open + escaped(cleanCss(css)) + close,
  );
}
