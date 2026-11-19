/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The contrast gate (DESIGN § color): resolves Prismoid's tokens through the stylesheets that
// define them - Propel's variables, @prismoid/blocks' accent, app.css (ADR-074) - converts
// oklch → linear sRGB, and asserts WCAG 2.x contrast on the fg/surface pairs the app
// actually renders. AA is the FLOOR (4.5:1 text · 3:1 UI); body-text pairs report their
// AAA (7:1) status. Zero dependencies - the conversion is the CSS Color 4 reference math.
// Run: `pnpm lint:contrast` (part of web-check). Exits 1 on any gate failure.

import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const blocksDir = join(here, "../../packages/blocks");
const propelDir = dirname(
  createRequire(join(blocksDir, "package.json")).resolve("@makeplane/propel/package.json"),
);
const read = (path) =>
  readFileSync(path, "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
const propel = read(join(propelDir, "dist/styles/variables.css"));
const blocks = read(join(blocksDir, "src/styles.css"));
const app = read(join(here, "../app/app.css"));

// ── the cascade, per scheme (brace-matched blocks, later wins) ───────────────
function block(css, opener) {
  const at = typeof opener === "string" ? css.indexOf(opener) : css.search(opener);
  if (at < 0) throw new Error(`no block ${opener}`);
  let depth = 0;
  for (let i = css.indexOf("{", at); i < css.length; i++) {
    if (css[i] === "{") depth++;
    if (css[i] === "}" && --depth === 0) return css.slice(css.indexOf("{", at) + 1, i);
  }
  throw new Error(`unterminated ${opener}`);
}
// Top-level custom properties only: a nested block (`@variant dark`) is its own scope.
function decls(body) {
  let flat = "";
  let depth = 0;
  for (const ch of body) {
    if (ch === "{") depth++;
    else if (ch === "}") depth--;
    else if (depth === 0) flat += ch;
  }
  return Object.fromEntries(
    [...flat.matchAll(/--([\w-]+):\s*([^;]+);/g)].map(([, n, v]) => [n, v.trim()]),
  );
}
const ROOT = /:root,\s*:host\s*\{/;
const propelRoot = block(propel, ROOT);
const blocksRoot = block(blocks, ROOT);
const lightVars = {
  ...decls(propelRoot),
  ...decls(blocksRoot),
  ...decls(block(app, ":root {")),
};
const darkVars = {
  ...lightVars,
  ...decls(block(propelRoot, "@variant dark {")),
  ...decls(block(blocksRoot, "@variant dark {")),
  ...decls(block(app, '[data-theme*="dark"] {')),
};

// var() chains down to an oklch() color; a non-color token never reaches the audit.
function resolve(vars, name, seen = []) {
  const value = vars[name];
  if (value === undefined) throw new Error(`unresolved --${[...seen, name].join(" → --")}`);
  const ref = value.match(/^var\(--([\w-]+)\)$/);
  if (ref) return resolve(vars, ref[1], [...seen, name]);
  const m = value.match(
    /^oklch\(([\d.]+)\s+([\d.]+)\s+([\d.]+)(?:\s*\/\s*([\d.]+%?))?\)$/,
  );
  if (!m) throw new Error(`--${name} is not an oklch color: ${value}`);
  const [, L, C, H, alpha] = m;
  return {
    L: +L,
    C: +C,
    H: +H,
    alpha: alpha ? parseFloat(alpha) / (alpha.endsWith("%") ? 100 : 1) : 1,
  };
}
const tokens = (vars) =>
  new Proxy({}, { get: (_, name) => (name in vars ? resolve(vars, name) : undefined) });

// ── oklch → linear sRGB (CSS Color 4 reference matrices) ─────────────────────
function linearRgb({ L, C, H }) {
  const a = C * Math.cos((H * Math.PI) / 180);
  const b = C * Math.sin((H * Math.PI) / 180);
  const l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3;
  const m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3;
  const s = (L - 0.0894841775 * a - 1.291485548 * b) ** 3;
  const clamp = (x) => Math.min(1, Math.max(0, x));
  return [
    clamp(+4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s),
    clamp(-1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s),
    clamp(-0.0041960863 * l - 0.7034186147 * m + 1.7076147009 * s),
  ];
}
const luminance = (rgb) =>
  0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2];
const over = (top, alpha, bottom) =>
  top.map((c, i) => c * alpha + bottom[i] * (1 - alpha));
function ratio(fgRgb, bgRgb) {
  const [a, b] = [luminance(fgRgb), luminance(bgRgb)].sort((x, y) => y - x);
  return (a + 0.05) / (b + 0.05);
}

// sanity: white on black must be 21:1
if (
  Math.abs(ratio(linearRgb({ L: 1, C: 0, H: 0 }), linearRgb({ L: 0, C: 0, H: 0 })) - 21) > 0.2
)
  throw new Error("conversion sanity check failed");

// ── the audited pairs ────────────────────────────────────────────────────────
const TEXT = 4.5; // AA normal text - the floor
const UI = 3; // AA UI components / large text
const AAA = 7; // reported for body pairs

function audit(scheme, t) {
  const rgb = (name) => {
    if (!t[name]) throw new Error(`${scheme}: missing --${name}`);
    return linearRgb(t[name]);
  };
  // solid surface under an alpha token (dark borders are white/α over the surface)
  const surface = (name, underName) =>
    t[name].alpha < 1 ? over(rgb(name), t[name].alpha, rgb(underName)) : rgb(name);
  const wash = (toneName, alpha, underName) =>
    over(rgb(toneName), alpha, rgb(underName));

  const failures = [];
  const rows = [];
  const check = (label, fg, bg, min, aaa = false) => {
    const r = ratio(fg, bg);
    const ok = r >= min;
    rows.push(
      `${ok ? " ✓" : " ✗"} ${label.padEnd(46)} ${r.toFixed(2).padStart(6)}  (≥${min}${
        aaa ? r >= AAA ? " · AAA" : " · aa-only" : ""
      })`,
    );
    if (!ok) failures.push(`${scheme}: ${label} = ${r.toFixed(2)} < ${min}`);
  };

  // body text on every reading surface - AAA reported
  for (const s of ["background", "card", "popover", "sidebar"])
    check(`foreground / ${s}`, rgb("foreground"), rgb(s), TEXT, true);
  // muted text on every surface it sits on
  for (const s of ["background", "card", "popover", "muted", "sidebar"])
    check(`muted-foreground / ${s}`, rgb("muted-foreground"), rgb(s), TEXT);
  // paired surfaces
  for (const [fg, bg] of [
    ["card-foreground", "card"],
    ["popover-foreground", "popover"],
    ["primary-foreground", "primary"],
    ["secondary-foreground", "secondary"],
    ["accent-foreground", "accent"],
    ["sidebar-foreground", "sidebar"],
    ["sidebar-accent-foreground", "sidebar-accent"],
    ["sidebar-primary-foreground", "sidebar-primary"],
    ["success-foreground", "success"],
    ["warning-foreground", "warning"],
    ["info-foreground", "info"],
  ])
    check(`${fg} / ${bg}`, rgb(fg), rgb(bg), TEXT);
  // hover rows: foreground over accent
  check("foreground / accent", rgb("foreground"), rgb("accent"), TEXT);
  // status tones AS TEXT (markers, pills, "waiting for signal…") on canvas + card,
  // and on their own /10 wash (TONE_TINT) over the canvas
  for (const tone of ["destructive", "success", "warning", "info"]) {
    check(`${tone} text / background`, rgb(tone), rgb("background"), TEXT);
    check(`${tone} text / card`, rgb(tone), rgb("card"), TEXT);
    check(
      `${tone} text / ${tone}-10-wash`,
      rgb(tone),
      wash(tone, 0.1, "background"),
      TEXT,
    );
  }
  // categorical ramp: full-strength text on its /10 wash over the card (collections)
  for (let i = 1; i <= 8; i++)
    check(
      `hue-${i} text / hue-${i}-10-wash`,
      rgb(`hue-${i}`),
      wash(`hue-${i}`, 0.10, "card"),
      TEXT,
    );
  // UI minimums: focus ring + solid primary control against the canvas
  check("ring / background (focus, UI)", surface("ring", "background"), rgb("background"), UI);
  check("primary / background (control, UI)", rgb("primary"), rgb("background"), UI);

  return { rows, failures };
}

const light = audit("light", tokens(lightVars));
const dark = audit("dark", tokens(darkVars));

for (const [name, r] of [
  ["light", light],
  ["dark", dark],
]) {
  console.log(`\n── ${name} ─────────────────────────────────────────────`);
  for (const row of r.rows) console.log(row);
}

const failures = [...light.failures, ...dark.failures];
if (failures.length) {
  console.error(`\n${failures.length} contrast failure(s):`);
  for (const f of failures) console.error("  " + f);
  process.exit(1);
}
console.log("\ncontrast: every gated pair passes AA.");
