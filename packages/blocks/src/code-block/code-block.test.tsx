/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  CodeBlock,
  CodeBlockActions,
  CodeBlockCopyButton,
  CodeBlockFilename,
  CodeBlockHeader,
  CodeBlockTitle,
} from "./index";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const code = 'const greeting = "hello";\nconsole.log(greeting);';

describe("CodeBlock", () => {
  it("shows the code at once, before any highlighting lands, with its language on the frame", () => {
    const { container } = render(<CodeBlock code={code} language="ts" />);
    expect(container.querySelector("pre")?.textContent).toContain('const greeting = "hello";');
    expect(container.querySelector("[data-language]")?.getAttribute("data-language")).toBe("ts");
  });

  it("a header carries the file name and the copy action", () => {
    render(
      <CodeBlock code={code} language="ts">
        <CodeBlockHeader>
          <CodeBlockTitle>
            <CodeBlockFilename>greet.ts</CodeBlockFilename>
          </CodeBlockTitle>
          <CodeBlockActions>
            <CodeBlockCopyButton />
          </CodeBlockActions>
        </CodeBlockHeader>
      </CodeBlock>,
    );
    expect(screen.getByText("greet.ts")).toBeDefined();
    expect(screen.getByRole("button", { name: "Copy code" })).toBeDefined();
  });
});

describe("CodeBlockCopyButton", () => {
  it("copies the block's code and reports it", async () => {
    const user = userEvent.setup();
    const writeText = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue();
    const onCopy = vi.fn();
    render(
      <CodeBlock code={code} language="ts">
        <CodeBlockCopyButton onCopy={onCopy} />
      </CodeBlock>,
    );
    await user.click(screen.getByRole("button", { name: "Copy code" }));
    await waitFor(() => expect(onCopy).toHaveBeenCalledTimes(1));
    expect(writeText).toHaveBeenCalledWith(code);
  });

  it("a clipboard that refuses is reported as an error, never thrown", async () => {
    const user = userEvent.setup();
    vi.spyOn(navigator.clipboard, "writeText").mockRejectedValue(new Error("denied"));
    const onError = vi.fn();
    render(
      <CodeBlock code={code} language="ts">
        <CodeBlockCopyButton onError={onError} aria-label="Copy snippet" />
      </CodeBlock>,
    );
    await user.click(screen.getByRole("button", { name: "Copy snippet" }));
    await waitFor(() => expect(onError).toHaveBeenCalledTimes(1));
    await act(async () => {});
    expect(onError).toHaveBeenCalledWith(expect.objectContaining({ message: "denied" }));
  });
});
