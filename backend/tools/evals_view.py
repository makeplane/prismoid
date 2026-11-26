# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A local, read-only viewer for the eval datasets, the run reports and the golden cases.

    just evals-view                                   # opens http://127.0.0.1:8765/
    just evals-view --reports ~/elsewhere --port 9000 --no-open

Standard library only; binds 127.0.0.1; never writes a file. Every request re-reads the files, so an
edited dataset or a new report shows up on the next refresh (the page also refreshes on focus).

The numbers (accuracy, strata, confusion, broke/fixed against a baseline) are never recomputed here:
the Numbers button runs `python -m evals.report` through `uv run --frozen`, and the golden tab imports
`evals.cases.golden` the same way. Dataset and report text is rendered with textContent only: rows hold
adversarial asks by design.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

GOLDEN_DUMP = (
    "import dataclasses, json\n"
    "from evals.cases.golden import CASES\n"
    "print(json.dumps([dataclasses.asdict(c) for c in CASES]))\n"
)


def find_repo(start: Path) -> Path:
    for p in (start, *start.parents):
        if (p / "backend" / "evals" / "datasets").is_dir():
            return p
    raise SystemExit(f"no prismoid checkout (backend/evals/datasets) at or above {start}; pass --repo")


class Viewer:
    def __init__(self, repo: Path, reports: list[Path]) -> None:
        self.repo = repo
        self.backend = repo / "backend"
        self.datasets_dir = self.backend / "evals" / "datasets"
        self.reports_dirs = reports or [self.backend / "evals" / "reports"]

    # ── files ────────────────────────────────────────────────────────────────────────────────
    def dataset_paths(self) -> dict[str, Path]:
        return {p.name: p for p in sorted(self.datasets_dir.glob("*.jsonl"))}

    def report_paths(self) -> dict[str, Path]:
        found: dict[str, Path] = {}
        for d in self.reports_dirs:
            if d.is_dir():
                for p in sorted(d.glob("*.json")):
                    found[str(p.resolve())] = p.resolve()
        return found

    def index(self) -> dict:
        datasets = []
        for name, p in self.dataset_paths().items():
            lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
            datasets.append({"name": name, "rows": len(lines), "mtime": p.stat().st_mtime})
        reports = []
        for key, p in self.report_paths().items():
            entry = {"id": key, "file": p.name, "dir": str(p.parent), "mtime": p.stat().st_mtime}
            try:
                r = json.loads(p.read_text(encoding="utf-8"))
                entry |= {
                    "suite": r.get("suite"),
                    "tree": r.get("tree"),
                    "date": r.get("date"),
                    "gate": r.get("gate"),
                    "trials": r.get("trials"),
                    "records": len(r.get("records") or []),
                    "partial": r.get("selection") is not None,
                }
            except (OSError, ValueError) as e:
                entry["error"] = str(e)
            reports.append(entry)
        reports.sort(key=lambda e: e["mtime"], reverse=True)
        return {
            "repo": str(self.repo),
            "reports_dirs": [str(d) for d in self.reports_dirs],
            "datasets": datasets,
            "reports": reports,
        }

    def dataset(self, name: str) -> dict:
        path = self.dataset_paths().get(name)
        if path is None:
            raise KeyError(name)
        rows, errors = [], []
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except ValueError as e:
                errors.append({"line": n, "error": str(e)})
        return {"name": name, "rows": rows, "errors": errors}

    def report(self, key: str) -> dict:
        path = self.report_paths().get(key)
        if path is None:
            raise KeyError(key)
        return json.loads(path.read_text(encoding="utf-8"))

    # ── the repo's own tools ─────────────────────────────────────────────────────────────────
    def _uv(self, *args: str, timeout: int = 600) -> dict:
        cmd = ["uv", "run", "--frozen", "--project", str(self.backend), "python", *args]
        try:
            done = subprocess.run(cmd, cwd=self.backend, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            return {"ok": False, "stderr": "uv is not on PATH", "cmd": " ".join(cmd)}
        except subprocess.TimeoutExpired:
            return {"ok": False, "stderr": f"timed out after {timeout}s", "cmd": " ".join(cmd)}
        return {
            "ok": done.returncode == 0,
            "stdout": done.stdout,
            "stderr": done.stderr,
            "cmd": " ".join(cmd),
        }

    def numbers(self, key: str, baseline: str | None) -> dict:
        paths = self.report_paths()
        if key not in paths or (baseline and baseline not in paths):
            raise KeyError(key)
        args = ["-m", "evals.report", str(paths[key])]
        if baseline:
            args += ["--baseline", str(paths[baseline])]
        md = self._uv(*args)
        if not md["ok"]:
            return md
        js = self._uv(*args, "--json")
        out = {"ok": True, "markdown": md["stdout"], "cmd": md["cmd"]}
        if js["ok"]:
            with contextlib.suppress(ValueError):
                out["json"] = json.loads(js["stdout"])
        return out

    def golden(self) -> dict:
        got = self._uv("-c", GOLDEN_DUMP)
        if got["ok"]:
            got["cases"] = json.loads(got.pop("stdout"))
        return got


def make_handler(viewer: Viewer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:  # quiet
            pass

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data: object, status: int = 200) -> None:
            self._send(status, json.dumps(data).encode(), "application/json")

        def do_GET(self) -> None:
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                if url.path == "/":
                    self._send(200, PAGE.encode(), "text/html; charset=utf-8")
                elif url.path == "/api/index":
                    self._json(viewer.index())
                elif url.path == "/api/dataset":
                    self._json(viewer.dataset(q["name"]))
                elif url.path == "/api/report":
                    self._json(viewer.report(q["id"]))
                elif url.path == "/api/numbers":
                    self._json(viewer.numbers(q["id"], q.get("baseline") or None))
                elif url.path == "/api/golden":
                    self._json(viewer.golden())
                else:
                    self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            except KeyError as e:
                self._json({"error": f"unknown {e}"}, HTTPStatus.NOT_FOUND)
            except Exception as e:  # a local tool: show the error rather than drop the socket
                self._json({"error": f"{type(e).__name__}: {e}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    return Handler


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>prismoid evals</title><link rel="icon" href="data:,">
<style>
:root{--bg:#fbfbfa;--fg:#1d1d1b;--mut:#6b6b66;--line:#e4e3df;--card:#fff;--acc:#3b5bdb;--ok:#2b8a3e;--bad:#c92a2a;--warn:#b76e00;--sel:#eef2ff}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#e8e6e1;--mut:#9a988f;--line:#2e2d2a;--card:#1e1e1c;--acc:#91a7ff;--ok:#69db7c;--bad:#ff8787;--warn:#ffc078;--sel:#252a3d}}
*{box-sizing:border-box}body{margin:0;font:13px/1.45 ui-sans-serif,system-ui,-apple-system,sans-serif;background:var(--bg);color:var(--fg)}
header{display:flex;gap:16px;align-items:center;padding:10px 16px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg);z-index:3}
header h1{font-size:14px;margin:0;font-weight:600}header .repo{color:var(--mut);font-size:12px;margin-left:auto;font-family:ui-monospace,monospace}
nav button{background:none;border:0;padding:6px 10px;border-radius:6px;color:var(--mut);cursor:pointer;font:inherit}
nav button.on{background:var(--sel);color:var(--fg);font-weight:600}
main{display:flex;min-height:calc(100vh - 45px)}
aside{width:210px;flex:none;border-right:1px solid var(--line);padding:10px}
aside .item{display:flex;justify-content:space-between;padding:5px 8px;border-radius:6px;cursor:pointer}
aside .item:hover{background:var(--sel)}aside .item.on{background:var(--sel);font-weight:600}aside .n{color:var(--mut);font-variant-numeric:tabular-nums}
section.view{flex:1;min-width:0;padding:10px 16px}
.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:10px}
input,select,button.b{font:inherit;color:var(--fg);background:var(--card);border:1px solid var(--line);border-radius:6px;padding:5px 8px}
button.b{cursor:pointer}button.b:hover{border-color:var(--acc)}input[type=search]{width:260px}
.count{color:var(--mut);margin-left:auto;font-variant-numeric:tabular-nums}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:8px}
th,td{text-align:left;padding:5px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-weight:600;color:var(--mut);font-size:12px;position:sticky;top:45px;background:var(--card);cursor:pointer;user-select:none;white-space:nowrap}
tbody tr{cursor:pointer}tbody tr:hover{background:var(--sel)}tr.on{background:var(--sel)}
td.id,td.mono,.mono{font-family:ui-monospace,monospace;font-size:12px}td.msg{max-width:520px}
td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.pill{display:inline-block;padding:0 6px;border-radius:10px;border:1px solid var(--line);font-size:11px;margin:1px 2px 1px 0;white-space:nowrap}
.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}.mut{color:var(--mut)}
.pill.ok{border-color:var(--ok)}.pill.bad{border-color:var(--bad)}.pill.warn{border-color:var(--warn)}
#detail{position:fixed;right:0;top:45px;bottom:0;width:min(560px,45vw);background:var(--card);border-left:1px solid var(--line);padding:12px 14px;overflow:auto;display:none;z-index:4;box-shadow:-8px 0 24px rgba(0,0,0,.08)}
#detail.open{display:block}#detail h2{font-size:13px;margin:0 0 8px;font-family:ui-monospace,monospace;word-break:break-all}
#detail h3{font-size:12px;color:var(--mut);margin:14px 0 4px;text-transform:uppercase;letter-spacing:.04em}
pre{white-space:pre-wrap;word-break:break-word;font:12px/1.45 ui-monospace,monospace;background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:8px;margin:0}
.close{float:right}.meta{display:flex;flex-wrap:wrap;gap:6px 14px;margin-bottom:10px;color:var(--mut)}.meta b{color:var(--fg);font-weight:600}
.empty{color:var(--mut);padding:30px;text-align:center}.err{color:var(--bad)}
.msgfull{white-space:pre-wrap}.nowrap{white-space:nowrap}pre.numbers{max-height:42vh;overflow:auto}
</style></head><body>
<header><h1>prismoid evals</h1><nav id="tabs"></nav><span class="repo" id="repo"></span></header>
<main><aside id="side"></aside><section class="view" id="view"></section></main>
<div id="detail"></div>
<script>
"use strict";
// Every piece of dataset or report text is set with textContent: rows hold adversarial asks by design.
const S = {tab:"datasets", index:null, ds:null, dsCache:{}, reportCache:{}, rows:[], sort:null, f:{}, open:null, numbers:{}, golden:null};
const $ = (s, r=document) => r.querySelector(s);
function el(tag, attrs={}, ...kids){
  const e = document.createElement(tag);
  for (const [k,v] of Object.entries(attrs)){
    if (v == null || v === false) continue;
    if (k === "class") e.className = v; else if (k === "text") e.textContent = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v); else e.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat()) if (k != null) e.append(k instanceof Node ? k : document.createTextNode(String(k)));
  return e;
}
const pill = (t, cls="") => el("span", {class:"pill "+cls, text:t});
async function api(path){ const r = await fetch(path); const j = await r.json(); if (!r.ok) throw new Error(j.error || r.status); return j; }
const fmt = (n, d=2) => n == null ? "–" : Number(n).toFixed(d);
const trunc = (s, n=160) => !s ? "" : (s.length > n ? s.slice(0, n) + "…" : s);

// ── state in the URL hash ─────────────────────────────────────────────────────
function saveHash(){
  const h = new URLSearchParams({tab:S.tab});
  if (S.ds) h.set("ds", S.ds);
  for (const [k,v] of Object.entries(S.f)) if (v) h.set(k, v);
  history.replaceState(null, "", "#" + h);
}
function loadHash(){
  const h = new URLSearchParams(location.hash.slice(1));
  S.tab = h.get("tab") || "datasets"; S.ds = h.get("ds");
  for (const [k,v] of h) if (!["tab","ds"].includes(k)) S.f[k] = v;
}

// ── data ───────────────────────────────────────────────────────────────────────
async function loadIndex(){ S.index = await api("/api/index"); $("#repo").textContent = S.index.repo; }
async function dataset(name, fresh){
  if (fresh || !S.dsCache[name]) S.dsCache[name] = await api("/api/dataset?name=" + encodeURIComponent(name));
  return S.dsCache[name];
}
async function report(id, fresh){
  if (!id) return null;
  if (fresh || !S.reportCache[id]) S.reportCache[id] = await api("/api/report?id=" + encodeURIComponent(id));
  return S.reportCache[id];
}
let rowIndex = null; // id -> {row, dataset}, over every dataset, to show a report row's definition
async function allRows(fresh){
  if (rowIndex && !fresh) return rowIndex;
  rowIndex = {};
  for (const d of S.index.datasets){ const got = await dataset(d.name, fresh); for (const r of got.rows) rowIndex[r.id] = {row:r, dataset:d.name}; }
  return rowIndex;
}
// Per-row view of a report's trials: counts what the file says, decides nothing.
function byRow(rep){
  const m = {};
  if (!rep) return m;
  const get = id => (m[id] ??= {trials:[], errors:[], stalled:[], notRun:null});
  for (const t of rep.records || []) get(t.row).trials.push(t);
  for (const e of rep.errors || []) get(e.row).errors.push(e.error);
  for (const e of rep.stalled || []) get(e.row).stalled.push(e.error);
  for (const [id, why] of Object.entries(rep.not_run || {})) get(id).notRun = why;
  for (const a of Object.values(m)){
    const t = a.trials; a.pass = t.filter(x => x.passed).length; a.n = t.length;
    a.timeouts = t.filter(x => x.timed_out).length;
    a.outcomes = {}; for (const x of t){ const o = x.outcome ?? "no ending"; a.outcomes[o] = (a.outcomes[o]||0) + 1; }
    const mean = k => t.length ? t.reduce((s,x) => s + (x[k] ?? 0), 0) / t.length : null;
    a.cost = mean("cost_usd"); a.lat = mean("latency_s"); a.tools = mean("tool_calls"); a.machines = mean("machines");
    a.expected = t[0]?.expected;
    a.status = a.n === 0 ? (a.errors.length ? "error" : a.stalled.length ? "stalled" : a.notRun ? "not run" : "–")
             : a.pass === a.n ? "pass" : a.pass === 0 ? "fail" : "flaky";
  }
  return m;
}
const STATUS_CLS = {pass:"ok", fail:"bad", flaky:"warn", error:"bad", stalled:"warn", "not run":"mut"};
function resultCell(a){
  if (!a) return el("td", {class:"mut", text:"–"});
  const td = el("td", {class:"num"});
  if (a.n) td.append(pill(`${a.pass}/${a.n}`, STATUS_CLS[a.status]));
  else td.append(pill(a.status, STATUS_CLS[a.status]));
  if (a.errors.length && a.n) td.append(pill("err", "bad"));
  return td;
}

// ── layout ─────────────────────────────────────────────────────────────────────
function renderTabs(){
  const nav = $("#tabs"); nav.replaceChildren();
  for (const [k, label] of [["datasets","Datasets"],["reports","Reports"],["golden","Golden cases"]])
    nav.append(el("button", {class: S.tab === k ? "on" : "", text:label, onclick:() => { S.tab = k; S.f = {}; S.sort = null; closeDetail(); render(); }}));
}
async function render(){
  renderTabs(); saveHash();
  const side = $("#side"), view = $("#view");
  const focused = document.activeElement?.matches?.("input[type=search]") ? document.activeElement.placeholder : null;
  side.style.display = S.tab === "datasets" ? "" : "none";
  try {
    if (S.tab === "datasets") await renderDatasets(side, view);
    else if (S.tab === "reports") await renderReports(view);
    else await renderGolden(view);
  } catch (e){ view.replaceChildren(el("div", {class:"empty err", text:String(e)})); }
  if (focused){ const i = [...view.querySelectorAll("input[type=search]")].find(x => x.placeholder === focused); if (i){ i.focus(); i.setSelectionRange(i.value.length, i.value.length); } }
}
function sel(name, options, value, onchange, blank="any"){
  const s = el("select", {title:name, onchange:e => onchange(e.target.value)});
  s.append(el("option", {value:"", text:`${name}: ${blank}`}));
  for (const o of options){ const [v, t] = Array.isArray(o) ? o : [o, o]; s.append(el("option", {value:v, text:`${name}: ${t}`, selected: v === value})); }
  return s;
}
const uniq = xs => [...new Set(xs.filter(x => x != null && x !== ""))].sort();
function sortRows(rows, cols){
  if (!S.sort) return rows;
  const c = cols.find(c => c.key === S.sort.key); if (!c) return rows;
  const v = r => { const x = c.sortv ? c.sortv(r) : r[c.key]; return x == null ? "" : x; };
  return [...rows].sort((a,b) => { const x = v(a), y = v(b); return (x < y ? -1 : x > y ? 1 : 0) * S.sort.dir; });
}
function table(cols, rows, onrow, isOn){
  const thead = el("thead", {}, el("tr", {}, cols.map(c => el("th", {
    text: c.label + (S.sort?.key === c.key ? (S.sort.dir > 0 ? " ▲" : " ▼") : ""),
    onclick:() => { S.sort = {key:c.key, dir: S.sort?.key === c.key ? -S.sort.dir : 1}; render(); }}))));
  const tbody = el("tbody");
  for (const r of sortRows(rows, cols)){
    const tr = el("tr", {class: isOn?.(r) ? "on" : "", onclick:() => onrow(r)});
    for (const c of cols){ const v = c.cell(r); tr.append(v instanceof Node && v.tagName === "TD" ? v : el("td", {class:c.cls || ""}, v ?? "")); }
    tbody.append(tr);
  }
  return el("table", {}, thead, tbody);
}

// ── datasets ───────────────────────────────────────────────────────────────────
async function renderDatasets(side, view){
  const list = S.index.datasets;
  if (!S.ds || !list.find(d => d.name === S.ds)) S.ds = list.find(d => d.name === "outcomes.jsonl")?.name || list[0]?.name;
  side.replaceChildren(el("div", {class:"mut", style:"padding:0 8px 6px", text:"datasets/"}),
    ...list.map(d => el("div", {class:"item" + (d.name === S.ds ? " on" : ""), onclick:() => { S.ds = d.name; S.f = {}; S.sort = null; closeDetail(); render(); }},
      el("span", {text:d.name.replace(/\.jsonl$/, "")}), el("span", {class:"n", text:d.rows}))));
  const data = await dataset(S.ds);
  const rep = await report(S.f.res);
  const res = byRow(rep);
  const rows = data.rows, f = S.f;
  const q = (f.q || "").toLowerCase();
  const shown = rows.filter(r =>
    (!q || [r.id, r.message, r.label_note, r.rule_note, JSON.stringify(r.prior_turns || r.context?.turns || "")].some(x => (x || "").toLowerCase().includes(q))) &&
    (!f.exp || r.expected_outcome === f.exp) &&
    (!f.cat || r.category === f.cat) && (!f.grp || r.group === f.grp) && (!f.var || r.variant === f.var) &&
    (!f.chk || (f.chk === "with" ? (r.checks || []).length : !(r.checks || []).length)) &&
    (!f.ctype || (r.checks || []).some(c => c.type === f.ctype)) &&
    (!f.st || res[r.id]?.status === f.st));
  const upd = k => v => { S.f[k] = v; render(); };
  const bar = el("div", {class:"bar"},
    el("input", {type:"search", placeholder:"search id, message, notes…", value:f.q || "", oninput:e => { S.f.q = e.target.value; clearTimeout(S.t); S.t = setTimeout(render, 150); }}),
    sel("expected", uniq(rows.map(r => r.expected_outcome)), f.exp, upd("exp")),
    sel("category", uniq(rows.map(r => r.category)), f.cat, upd("cat")),
    sel("group", uniq(rows.map(r => r.group)), f.grp, upd("grp")),
    sel("variant", uniq(rows.map(r => r.variant)), f.var, upd("var")),
    sel("checks", [["with","with checks"],["without","without checks"]], f.chk, upd("chk")),
    sel("check type", uniq(rows.flatMap(r => (r.checks || []).map(c => c.type))), f.ctype, upd("ctype")),
    sel("results from", S.index.reports.map(r => [r.id, `${r.suite} @${r.tree} ${r.date}${r.partial ? " (selection)" : ""}`]), f.res, v => { S.f.res = v; S.f.st = ""; render(); }, "none"),
    f.res ? sel("result", ["pass","fail","flaky","error","stalled","not run"], f.st, upd("st")) : null,
    el("span", {class:"count", text:`${shown.length} of ${rows.length} rows`}));
  const cols = [
    {key:"id", label:"id", cls:"id", cell:r => r.id},
    {key:"message", label:"message", cls:"msg", cell:r => trunc(r.message)},
    {key:"expected_outcome", label:"expected", cell:r => pill(r.expected_outcome)},
    {key:"allowed", label:"also ok", sortv:r => (r.allowed_outcomes || []).join(), cell:r => (r.allowed_outcomes || []).map(o => pill(o, "mut"))},
    {key:"category", label:"category / group / variant", sortv:r => [r.category, r.group, r.variant].join(), cell:r => [r.category, r.group, r.variant].filter(Boolean).join(" · ")},
    {key:"checks", label:"checks", sortv:r => (r.checks || []).length, cell:r => (r.checks || []).map(c => pill(c.type))},
    {key:"fresh_as_of", label:"fresh", cls:"mono nowrap", cell:r => r.fresh_as_of || ""},
  ];
  if (rep) cols.push({key:"result", label:"result", sortv:r => res[r.id]?.status || "", cell:r => resultCell(res[r.id])});
  view.replaceChildren(bar,
    data.errors.length ? el("div", {class:"err", text:`${data.errors.length} line(s) are not JSON: ` + data.errors.map(e => `line ${e.line}: ${e.error}`).join("; ")}) : "",
    shown.length ? table(cols, shown, r => openRow(r, S.ds, rep, res[r.id]), r => S.open === r.id) : el("div", {class:"empty", text:"no rows match"}));
}

// ── detail drawer ──────────────────────────────────────────────────────────────
function closeDetail(){ S.open = null; $("#detail").classList.remove("open"); }
function openRow(row, dsName, rep, agg, baseAgg){
  S.open = row?.id || agg?.id;
  document.querySelectorAll("tbody tr.on").forEach(t => t.classList.remove("on"));
  const d = $("#detail"); d.replaceChildren(); d.classList.add("open");
  d.append(el("button", {class:"b close", text:"✕", onclick:closeDetail}), el("h2", {text: S.open}));
  if (row){
    d.append(el("div", {class:"meta"}, el("span", {}, "dataset ", el("b", {text:dsName})),
      el("span", {}, "expected ", el("b", {text:row.expected_outcome})),
      row.allowed_outcomes?.length ? el("span", {}, "also ok ", el("b", {text:row.allowed_outcomes.join(", ")})) : ""));
    const turns = row.prior_turns?.length ? row.prior_turns : row.context?.turns;
    if (turns?.length){ d.append(el("h3", {text:"earlier turns"})); for (const t of turns) d.append(el("pre", {class:"msgfull", style:"margin-bottom:4px", text: typeof t === "string" ? t : JSON.stringify(t, null, 2)})); }
    d.append(el("h3", {text:"message"}), el("pre", {class:"msgfull", text:row.message}));
    if (row.label_note) d.append(el("h3", {text:"label note"}), el("div", {text:row.label_note}));
    if (row.checks?.length) d.append(el("h3", {text:"checks"}), el("pre", {text:row.checks.map(c => JSON.stringify(c)).join("\n")}));
    if (row.judge_rubric) d.append(el("h3", {text:"judge rubric"}), el("pre", {class:"msgfull", text:row.judge_rubric}));
  } else d.append(el("div", {class:"warn", text:"this row id is in no dataset file in this checkout"}));
  for (const [label, a] of [["this run", agg], ["baseline", baseAgg]]){
    if (!a) continue;
    d.append(el("h3", {text:`result: ${label}`}));
    if (a.notRun) d.append(el("div", {class:"mut", text:"not run: " + a.notRun}));
    for (const e of a.errors) d.append(el("div", {class:"err", text:"infra error: " + e}));
    for (const e of a.stalled) d.append(el("div", {class:"warn", text:"stalled: " + e}));
    if (a.trials.length) d.append(el("pre", {text:a.trials.map((t, i) => `#${i+1} ${t.passed ? "PASS" : "FAIL"}${t.timed_out ? " (timed out)" : ""}  got=${t.outcome ?? "no ending"}  $${fmt(t.cost_usd, 4)}  ${fmt(t.latency_s, 1)}s  tools=${t.tool_calls}  machines=${t.machines}  undecided=${t.undecided}`).join("\n")}));
    if (a.trials.length) d.append(el("div", {class:"mut", style:"margin-top:4px", text:"Reports keep the verdict and numbers per trial, not the delivered text or which check failed."}));
  }
  d.append(el("h3", {text:"raw row"}), el("pre", {text: row ? JSON.stringify(row, null, 2) : "–"}));
  render();
}

// ── reports ────────────────────────────────────────────────────────────────────
async function renderReports(view){
  const reps = S.index.reports;
  if (!reps.length){
    view.replaceChildren(el("div", {class:"empty"}, el("div", {text:"No reports yet."}),
      el("div", {class:"mono", style:"margin-top:8px", text:"looked in: " + S.index.reports_dirs.join(", ")}),
      el("div", {style:"margin-top:8px", text:"A live run writes evals/reports/<suite>-<tree>.json; pass --reports DIR to look elsewhere."})));
    return;
  }
  const f = S.f;
  if (!f.r || !reps.find(r => r.id === f.r)) f.r = reps[0].id;
  const meta = reps.find(r => r.id === f.r);
  const rep = await report(f.r), base = await report(f.b);
  const idx = await allRows();
  const res = byRow(rep), bres = byRow(base);
  const nums = S.numbers[f.r + "|" + (f.b || "")];
  const changes = {};
  for (const c of nums?.json?.against?.broken || []) changes[c.row] = "broke";
  for (const c of nums?.json?.against?.fixed || []) changes[c.row] = "fixed";
  const upd = k => v => { S.f[k] = v; render(); };
  const label = r => `${r.suite} @${r.tree} ${r.date}${r.partial ? " (selection)" : ""} — ${r.file}`;
  const q = (f.q || "").toLowerCase();
  const ids = Object.keys(res);
  const shown = ids.filter(id => {
    const a = res[id], row = idx[id]?.row;
    return (!q || id.toLowerCase().includes(q) || (row?.message || "").toLowerCase().includes(q)) &&
      (!f.st || a.status === f.st) && (!f.ch || !nums?.json || changes[id] === f.ch) &&
      (!f.got || Object.keys(a.outcomes).includes(f.got));
  }).map(id => ({id, ...res[id]}));
  const bar1 = el("div", {class:"bar"},
    sel("report", reps.map(r => [r.id, label(r)]), f.r, v => { S.f = {r:v, b:S.f.b}; render(); }, "–"),
    sel("baseline", reps.filter(r => r.id !== f.r && r.suite === meta.suite).map(r => [r.id, label(r)]), f.b, upd("b"), "none"),
    el("button", {class:"b", text: nums ? "Recompute numbers" : "Numbers (evals.report)", onclick: async e => {
      e.target.disabled = true; e.target.textContent = "running evals.report…";
      const qs = "id=" + encodeURIComponent(f.r) + (f.b ? "&baseline=" + encodeURIComponent(f.b) : "");
      try { S.numbers[f.r + "|" + (f.b || "")] = await api("/api/numbers?" + qs); }
      catch (err){ S.numbers[f.r + "|" + (f.b || "")] = {ok:false, stderr:String(err)}; }
      render(); }}));
  const m = el("div", {class:"meta"},
    ...[["suite", rep.suite], ["kind", rep.kind], ["tree", rep.tree], ["date", rep.date], ["gate", rep.gate], ["trials (k)", rep.trials],
        ["prompts", (rep.prompts_sha || "").slice(0, 12)], ["records", (rep.records || []).length], ["errors", (rep.errors || []).length],
        ["stalled", (rep.stalled || []).length], ["not run", Object.keys(rep.not_run || {}).length], ["retried", rep.retried]]
      .map(([k, v]) => el("span", {}, k + " ", el("b", {class: k === "gate" ? ({pass:"ok", fail:"bad", inconclusive:"warn"}[v] || "") : "", text:String(v ?? "–")}))),
    rep.selection ? el("span", {class:"warn", text:"partial run (a selection of rows)"}) : "");
  const bar2 = el("div", {class:"bar"},
    el("input", {type:"search", placeholder:"search row id or message…", value:f.q || "", oninput:e => { S.f.q = e.target.value; clearTimeout(S.t); S.t = setTimeout(render, 150); }}),
    sel("result", ["pass","fail","flaky","error","stalled","not run"], f.st, upd("st")),
    sel("delivered", uniq(Object.values(res).flatMap(a => Object.keys(a.outcomes))), f.got, upd("got")),
    Object.keys(changes).length ? sel("change", [["broke","broke"],["fixed","fixed"]], f.ch, upd("ch")) : null,
    el("span", {class:"count", text:`${shown.length} of ${ids.length} rows`}));
  const cols = [
    {key:"id", label:"row", cls:"id", cell:r => r.id},
    {key:"message", label:"message", cls:"msg", sortv:r => idx[r.id]?.row?.message || "", cell:r => trunc(idx[r.id]?.row?.message, 110)},
    {key:"expected", label:"expected", sortv:r => r.expected || idx[r.id]?.row?.expected_outcome, cell:r => pill(r.expected || idx[r.id]?.row?.expected_outcome || "–")},
    {key:"got", label:"delivered", sortv:r => Object.keys(r.outcomes).join(), cell:r => Object.entries(r.outcomes).map(([o, n]) => pill(r.n > 1 ? `${o} ×${n}` : o, o === r.expected ? "" : "mut"))},
    {key:"status", label:"result", cell:r => resultCell(r)},
    {key:"cost", label:"$ / trial", cls:"num", cell:r => fmt(r.cost, 4)},
    {key:"lat", label:"latency s", cls:"num", cell:r => fmt(r.lat, 1)},
    {key:"tools", label:"tools", cls:"num", cell:r => fmt(r.tools, 1)},
    {key:"machines", label:"mach.", cls:"num", cell:r => fmt(r.machines, 1)},
  ];
  if (base){
    cols.push({key:"base", label:"baseline", sortv:r => bres[r.id]?.status || "", cell:r => resultCell(bres[r.id])});
    cols.push({key:"change", label:"change", sortv:r => changes[r.id] || "", cell:r => changes[r.id] ? pill(changes[r.id], changes[r.id] === "broke" ? "bad" : "ok") : ""});
  }
  const parts = [bar1, m];
  if (nums){
    if (!nums.ok) parts.push(el("pre", {class:"err", style:"margin-bottom:10px", text:(nums.cmd ? nums.cmd + "\n\n" : "") + (nums.stderr || nums.error || "failed")}));
    else parts.push(el("details", {open:true, style:"margin-bottom:12px"}, el("summary", {style:"cursor:pointer;color:var(--mut)", text:"Numbers from evals.report" + (f.b ? " (against the baseline)" : "")}), el("pre", {class:"numbers", text:nums.markdown})));
  } else if (base) parts.push(el("div", {class:"mut", style:"margin-bottom:8px", text:"Press Numbers to get broke/fixed and the paired deltas from evals.report; the baseline column only shows what that file recorded."}));
  parts.push(bar2, shown.length ? table(cols, shown, r => openRow(idx[r.id]?.row, idx[r.id]?.dataset, rep, res[r.id], base ? bres[r.id] : null), r => S.open === r.id) : el("div", {class:"empty", text:"no rows match"}));
  view.replaceChildren(...parts);
}

// ── golden ─────────────────────────────────────────────────────────────────────
async function renderGolden(view){
  if (!S.golden){
    view.replaceChildren(el("div", {class:"empty"},
      el("div", {text:"The golden cases live in code (evals/cases/golden.py), so reading them means importing that module."}),
      el("button", {class:"b", style:"margin-top:10px", text:"Load golden cases (uv run)", onclick: async e => {
        e.target.disabled = true; e.target.textContent = "importing…";
        try { S.golden = await api("/api/golden"); } catch (err){ S.golden = {ok:false, stderr:String(err)}; }
        render(); }})));
    return;
  }
  if (!S.golden.ok){ view.replaceChildren(el("pre", {class:"err", text:(S.golden.cmd || "") + "\n\n" + (S.golden.stderr || "failed")}), el("button", {class:"b", style:"margin-top:8px", text:"Retry", onclick:() => { S.golden = null; render(); }})); return; }
  const cases = S.golden.cases, f = S.f, q = (f.q || "").toLowerCase();
  const shown = cases.filter(c => (!q || (c.name + " " + c.goal).toLowerCase().includes(q)) && (!f.skill || c.skill === f.skill));
  const bar = el("div", {class:"bar"},
    el("input", {type:"search", placeholder:"search name or goal…", value:f.q || "", oninput:e => { S.f.q = e.target.value; clearTimeout(S.t); S.t = setTimeout(render, 150); }}),
    sel("skill", uniq(cases.map(c => c.skill)), f.skill, v => { S.f.skill = v; render(); }),
    el("button", {class:"b", text:"Reload", onclick:() => { S.golden = null; render(); }}),
    el("span", {class:"count", text:`${shown.length} of ${cases.length} cases`}));
  const cols = [
    {key:"name", label:"name", cls:"id", cell:c => c.name},
    {key:"goal", label:"goal", cls:"msg", cell:c => trunc(c.goal, 200)},
    {key:"skill", label:"skill", cell:c => c.skill ? pill(c.skill) : ""},
    {key:"inputs", label:"inputs", cls:"mono", sortv:c => (c.inputs || []).join(), cell:c => (c.inputs || []).map(p => p.split("/").pop()).join(", ")},
    {key:"must", label:"must contain", sortv:c => (c.must_contain || []).length, cell:c => (c.must_contain || []).map(t => pill(t, "mut"))},
  ];
  view.replaceChildren(bar, table(cols, shown, c => {
    S.open = c.name; const d = $("#detail"); d.replaceChildren(el("button", {class:"b close", text:"✕", onclick:closeDetail}), el("h2", {text:c.name}),
      el("h3", {text:"goal"}), el("pre", {class:"msgfull", text:c.goal}), el("h3", {text:"raw case"}), el("pre", {text:JSON.stringify(c, null, 2)}));
    d.classList.add("open"); render();
  }, c => S.open === c.name));
}

// ── boot ───────────────────────────────────────────────────────────────────────
document.addEventListener("keydown", e => { if (e.key === "Escape") closeDetail(); });
window.addEventListener("focus", async () => { await loadIndex(); S.dsCache = {}; S.reportCache = {}; rowIndex = null; render(); });
(async () => { loadHash(); try { await loadIndex(); render(); } catch (e){ $("#view").textContent = String(e); } })();
</script></body></html>
"""


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--repo", type=Path, help="a prismoid checkout (default: the one holding this file)")
    p.add_argument(
        "--reports",
        type=Path,
        action="append",
        default=[],
        help="a reports dir (repeatable; default <repo>/backend/evals/reports)",
    )
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-open", action="store_true", help="do not open a browser tab")
    args = p.parse_args()
    repo = (args.repo or find_repo(Path(__file__).resolve().parent)).expanduser().resolve()
    if not (repo / "backend" / "evals" / "datasets").is_dir():
        raise SystemExit(f"{repo} has no backend/evals/datasets")
    viewer = Viewer(repo, [d.expanduser().resolve() for d in args.reports])
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(viewer))
    url = f"http://127.0.0.1:{args.port}/"
    print(f"prismoid evals viewer: {url}  (repo {repo})", file=sys.stderr)
    if not args.no_open and not os.environ.get("EVALS_VIEW_NO_OPEN"):
        webbrowser.open(url)
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()


if __name__ == "__main__":
    main()
