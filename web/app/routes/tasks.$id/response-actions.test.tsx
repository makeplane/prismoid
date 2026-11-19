/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { ResponseActions } from "./response-actions";

const base = {
  answerText: "ok",
  feedback: null,
  onFeedback: () => {},
  retrying: false,
};

describe("ResponseActions", () => {
  it("an editor can Retry - it runs the ask again as a new turn", () => {
    expect(renderToStaticMarkup(<ResponseActions {...base} onRetry={() => {}} />)).toContain(
      "Retry",
    );
  });

  it("an editor grades the answer", () => {
    const html = renderToStaticMarkup(<ResponseActions {...base} onRetry={() => {}} />);
    expect(html).toContain('aria-label="Good response"');
    expect(html).toContain('aria-label="Bad response"');
  });

  it("a viewer keeps Copy and is offered neither the thumbs nor Retry (the server refuses both)", () => {
    const html = renderToStaticMarkup(<ResponseActions {...base} onFeedback={undefined} />);
    expect(html).not.toContain("Retry");
    expect(html).not.toContain('aria-label="Good response"');
    expect(html).toContain('aria-label="Copy"');
  });
});
