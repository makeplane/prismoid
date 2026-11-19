/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  Attachment,
  AttachmentAction,
  AttachmentActions,
  AttachmentContent,
  AttachmentDescription,
  AttachmentGroup,
  AttachmentMedia,
  AttachmentTitle,
  AttachmentTrigger,
} from "./index";

afterEach(cleanup);

describe("Attachment", () => {
  it("wears its state, size and orientation as data attributes, with done/default/horizontal defaults", () => {
    render(
      <AttachmentGroup data-testid="group">
        <Attachment data-testid="a" />
        <Attachment data-testid="b" state="error" size="xs" orientation="vertical" />
      </AttachmentGroup>,
    );
    const a = screen.getByTestId("a");
    expect(a.dataset).toMatchObject({
      slot: "attachment",
      state: "done",
      size: "default",
      orientation: "horizontal",
    });
    expect(screen.getByTestId("b").dataset).toMatchObject({
      state: "error",
      size: "xs",
      orientation: "vertical",
    });
    expect(screen.getByTestId("group").dataset.slot).toBe("attachment-group");
  });

  it("composes media, title, description and a named remove action that clicks", async () => {
    const onRemove = vi.fn();
    render(
      <Attachment>
        <AttachmentMedia variant="image" data-testid="media" />
        <AttachmentContent>
          <AttachmentTitle>report.pdf</AttachmentTitle>
          <AttachmentDescription>2.1 MB</AttachmentDescription>
        </AttachmentContent>
        <AttachmentActions>
          <AttachmentAction aria-label="Remove report.pdf" onClick={onRemove}>
            x
          </AttachmentAction>
        </AttachmentActions>
      </Attachment>,
    );
    expect(screen.getByTestId("media").dataset).toMatchObject({
      slot: "attachment-media",
      variant: "image",
    });
    expect(screen.getByText("report.pdf").dataset.slot).toBe("attachment-title");
    expect(screen.getByText("2.1 MB").dataset.slot).toBe("attachment-description");
    await userEvent.setup().click(screen.getByRole("button", { name: "Remove report.pdf" }));
    expect(onRemove).toHaveBeenCalledTimes(1);
  });

  it("the trigger is a type=button button by default, and keeps a type a render sets", () => {
    render(
      <>
        <AttachmentTrigger aria-label="Open" />
        <AttachmentTrigger aria-label="Link" render={<a href="/f" />} />
      </>,
    );
    expect(screen.getByRole("button", { name: "Open" }).getAttribute("type")).toBe("button");
    expect(screen.getByRole("link", { name: "Link" }).getAttribute("type")).toBeNull();
  });
});
