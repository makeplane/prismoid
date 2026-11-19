# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Every script the verbs run in a page, through `Runtime.callFunctionOn`: fixed constants, never
built from model text - the computer-use spec bans model-facing JavaScript, so a value a script
needs is handed over as a call argument, data, never spliced into the code."""

from __future__ import annotations

from typing import Final

# A function DECLARATION, as `Runtime.callFunctionOn` requires: Chromium calls it with `this`
# bound to the element. (An immediately-invoked `(function(el){...})(this)` ran once against the
# global object instead, threw, and every inspect answered "no form".) `value` is a select's
# candidate choice, handed over as a call argument - data, never spliced into the code.
# `submits`: a click on this element sends its form, as an Enter in one of its fields would - a
# submit button (a button's `type` reads "submit" when the page gave it none) or an image input -
# and then it, not the form's first submit control, is the submit control that acts.
# `form_labels` are the fields a person fills, never the form's other buttons or its hidden inputs:
# a product form's "Buy now" button is another act, so clicking its "Add to cart" never reads it.
# Only what the form sends counts: a disabled field or an unticked box or radio is never sent.
# `navigates`: this is a link whose click can only load another page (an http(s) href to another
# document, no inline handler); `form_navigates`: the page says its form sends by GET (a `method`
# of get, no inline `onsubmit`, no `formmethod` on its submit control). Neither is guessed: an
# unsaid method, a `#` or script href, or a handler answers false.
INSPECT_JS = """function(value) {
  const out = {form_labels: [], submit_name: "", option: "", submits: false,
               navigates: false, form_navigates: false};
  if (value !== undefined && this.options) {
    const o = Array.from(this.options).find((x) => x.value === value);
    if (o) out.option = o.text.trim();
  }
  if (this.tagName === "A" && this.getAttribute("href") !== null && this.getAttribute("onclick") === null) {
    const u = new URL(this.href, location.href);
    const web = u.protocol === "http:" || u.protocol === "https:";
    out.navigates = web && u.href.split("#")[0] !== location.href.split("#")[0];
  }
  const form = this.form || this.closest("form");
  if (!form) return out;
  const kind = this.tagName === "BUTTON" || this.tagName === "INPUT" ? this.type : "";
  out.submits = kind === "submit" || kind === "image";
  for (const c of form.querySelectorAll("input,select,textarea")) {
    if (["hidden", "submit", "image", "button", "reset"].includes(c.type) || c.disabled) continue;
    if ((c.type === "checkbox" || c.type === "radio") && !c.checked) continue;
    const l = c.labels && c.labels[0] ? c.labels[0].textContent : (c.getAttribute("aria-label") || c.getAttribute("placeholder") || c.name || "");
    if (l && out.form_labels.length < 40) out.form_labels.push(l.trim());
  }
  const s = out.submits ? this : form.querySelector("button:not([type=button]),input[type=submit],button[type=submit]");
  out.submit_name = s ? (s.getAttribute("aria-label") || s.value || s.textContent || "").trim() : "";
  const method = (s && s.getAttribute("formmethod")) || form.getAttribute("method") || "";
  out.form_navigates = method.toLowerCase() === "get" && form.getAttribute("onsubmit") === null;
  return out;
}"""


# The inputs Chromium takes only WHOLE, in their own format: typed text never lands in a date or
# a time picker (`Input.insertText` is ignored; a delivery time the
# computer could not set in twenty keystrokes). `type`/`fill` set their value and read it back;
# an empty read-back means the format was wrong, and the verb says which format the field takes.
WHOLE_VALUE_FORMATS: Final = {
    "date": "YYYY-MM-DD",
    "time": "HH:MM, 24-hour",
    "datetime-local": "YYYY-MM-DDTHH:MM",
    "month": "YYYY-MM",
    "week": "YYYY-Www",
    "color": "#rrggbb",
    "range": "a number",
}
# The snapshot shows a picker's parts ("spinbutton Hours") inside the input's own shadow root, and
# the model aims at a part: the part's shadow host is the input that takes the value.
WHOLE_VALUE_JS = """function(v) {
  const whole = ["date", "time", "datetime-local", "month", "week", "color", "range"];
  let el = this;
  const root = el.getRootNode ? el.getRootNode() : null;
  if (el.tagName !== "INPUT" && root && root.host && root.host.tagName === "INPUT") el = root.host;
  if (el.tagName !== "INPUT" || !whole.includes(el.type)) return {whole: false};
  el.value = v;
  el.dispatchEvent(new Event("input", {bubbles: true}));
  el.dispatchEvent(new Event("change", {bubbles: true}));
  return {whole: true, type: el.type, value: el.value};
}"""


# Installs its observer once per document (a new document is a new world), then reads; with a
# `capMs`, it answers once the page has been quiet for `quietMs`, or at the cap.
PROBE_JS: Final = """function(quietMs, capMs) {
  if (!globalThis.__prismoidWatch) {
    const s = {doc: Math.random().toString(36).slice(2), n: 0, last: performance.now()};
    new MutationObserver((records) => { s.n += records.length; s.last = performance.now(); })
      .observe(document, {subtree: true, childList: true, attributes: true, characterData: true});
    globalThis.__prismoidWatch = s;
  }
  const s = globalThis.__prismoidWatch;
  const read = () => ({doc: s.doc, n: s.n, url: location.href, x: scrollX, y: scrollY,
                       loading: document.readyState === "loading"});
  if (!capMs) return read();
  const start = performance.now();
  return new Promise((done) => {
    const tick = () => {
      const now = performance.now();
      if (now - s.last >= quietMs || now - start >= capMs) done(read());
      else setTimeout(tick, 16);
    };
    tick();
  });
}"""


# Answers once `text` shows on the page (rendered text, as a person reads it), or false at `capMs`:
# every mutation re-reads, so it answers when the page changes and never polls.
APPEARS_JS: Final = """function(text, capMs) {
  const seen = () => document.body !== null && document.body.innerText.includes(text);
  if (seen()) return true;
  return new Promise((done) => {
    const stop = (found) => { observer.disconnect(); clearTimeout(timer); done(found); };
    const observer = new MutationObserver(() => { if (seen()) stop(true); });
    observer.observe(document, {subtree: true, childList: true, attributes: true, characterData: true});
    const timer = setTimeout(() => stop(false), capMs);
  });
}"""
