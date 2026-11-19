/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { StatusDot, StatusMark, statusLabel } from "./status-badge";

describe("StatusDot", () => {
  it("names the status when the dot carries meaning", () => {
    const html = renderToStaticMarkup(<StatusDot status="running" />);
    expect(html).toContain('role="img"');
    expect(html).toContain('aria-label="Status: running"');
  });

  it("is hidden when adjacent text already names the status", () => {
    const html = renderToStaticMarkup(<StatusDot status="delivered" decorative />);
    expect(html).toContain('aria-hidden="true"');
    expect(html).not.toContain("aria-label");
  });

  it("ripples while live - machine motion and gates waiting on you, nothing settled", () => {
    for (const s of ["running", "waiting"]) {
      expect(renderToStaticMarkup(<StatusDot status={s} />)).toContain("animate-status-ping");
    }
    for (const s of ["delivered", "failed", "created"]) {
      expect(renderToStaticMarkup(<StatusDot status={s} />)).not.toContain("animate-status-ping");
    }
  });

  it("not-yet-run is hollow, settled is solid", () => {
    const queued = renderToStaticMarkup(<StatusDot status="created" />);
    expect(queued).toContain("ring-inset");
    expect(queued).not.toContain("bg-current");
    const done = renderToStaticMarkup(<StatusDot status="delivered" />);
    expect(done).toContain("bg-current");
    expect(done).not.toContain("ring-inset");
  });
});

describe("StatusMark labelled", () => {
  it("says Needs you beside the glyph of a task waiting on a person", () => {
    const html = renderToStaticMarkup(<StatusMark status="waiting" labelled />);
    expect(html).toContain("Needs you");
  });

  it("stays a bare glyph for states with no words, and unless asked", () => {
    expect(renderToStaticMarkup(<StatusMark status="delivered" labelled />)).not.toContain(
      "Needs you",
    );
    expect(renderToStaticMarkup(<StatusMark status="waiting" />)).not.toContain("Needs you");
  });
});

describe("StatusMark", () => {
  it("spins while working, stands still when settled", () => {
    const live = renderToStaticMarkup(<StatusMark status="running" />);
    expect(live).toContain("animate-liveness"); // the fade trio, not the arc
    expect(live).toContain('aria-label="Status: running"');
    const done = renderToStaticMarkup(<StatusMark status="delivered" />);
    expect(done).not.toContain("animate-liveness");
    expect(done).toContain("text-success");
  });

  it("falls still over an empty category - motion must have something to report", () => {
    const empty = renderToStaticMarkup(<StatusMark status="running" still />);
    expect(empty).not.toContain("animate-liveness");
    expect(empty).toContain("text-info"); // glyph and tone stay; only the motion stops
  });

  it("marks needs-input and queued distinctly, honours decorative", () => {
    expect(renderToStaticMarkup(<StatusMark status="waiting" />)).toContain("text-warning");
    // "created" wears the human word everywhere it surfaces
    expect(renderToStaticMarkup(<StatusMark status="created" />)).toContain('title="queued"');
    const deco = renderToStaticMarkup(<StatusMark status="delivered" decorative />);
    expect(deco).toContain('aria-hidden="true"');
    expect(deco).not.toContain("aria-label");
  });
});

describe("statusLabel", () => {
  it("says queued for created", () => {
    expect(statusLabel("created")).toBe("queued");
  });

  it("leaves other statuses human-readable", () => {
    expect(statusLabel("running")).toBe("running");
    expect(statusLabel("waiting")).toBe("waiting");
  });
});
