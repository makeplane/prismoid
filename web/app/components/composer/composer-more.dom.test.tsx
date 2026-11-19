/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// @vitest-environment happy-dom
// The rest of the composer: staging text from outside the box, attaching and removing files,
// a failed attachment blocking the send, files dropped on the window, dictation, the camera
// dialog, and the busy states of the send button.
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createRef, type MutableRefObject } from "react";
import { toast } from "@prismoid/blocks/toast";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Composer, type ComposerApi } from "./composer";

beforeEach(() => {
  URL.createObjectURL = vi.fn(() => "blob:p");
  URL.revokeObjectURL = vi.fn();
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  delete (window as unknown as Record<string, unknown>).webkitSpeechRecognition;
  document.body.innerHTML = "";
});

const box = () => screen.getByRole("textbox") as HTMLTextAreaElement;
const fileInput = () => document.querySelector("input[type=file]") as HTMLInputElement;

describe("Composer driven from outside", () => {
  it("insert stages text after what is there, clear only drops an unedited draft", async () => {
    const user = userEvent.setup();
    const apiRef = createRef<ComposerApi | null>() as MutableRefObject<ComposerApi | null>;
    const seen: string[] = [];
    render(<Composer onSubmit={vi.fn()} apiRef={apiRef} onTextChange={(t) => seen.push(t)} />);
    await user.type(box(), "first");
    act(() => apiRef.current?.insert("second"));
    expect(box().value).toBe("first\n\nsecond");
    act(() => apiRef.current?.clear("something else"));
    expect(box().value).toBe("first\n\nsecond");
    act(() => apiRef.current?.clear("first\n\nsecond"));
    expect(box().value).toBe("");
    expect(seen.at(-1)).toBe("");
    expect(seen).toContain("first\n\nsecond");
  });

  it("the handle goes away with the composer", () => {
    const apiRef = createRef<ComposerApi | null>() as MutableRefObject<ComposerApi | null>;
    const { unmount } = render(<Composer onSubmit={vi.fn()} apiRef={apiRef} />);
    expect(apiRef.current).not.toBeNull();
    unmount();
    expect(apiRef.current).toBeNull();
  });
});

describe("Composer attachments", () => {
  it("an attached file shows as a chip, sends with the text, and clears after", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    render(<Composer onSubmit={onSubmit} />);
    await user.upload(fileInput(), new File(["hello"], "notes.txt", { type: "text/plain" }));
    expect(await screen.findByText("notes.txt")).toBeDefined();
    await user.type(box(), "see file{Enter}");
    expect(onSubmit).toHaveBeenCalledTimes(1);
    const sent = onSubmit.mock.calls[0][0];
    expect(sent.text).toBe("see file");
    expect(sent.attachments[0]).toMatchObject({ filename: "notes.txt", content: btoa("hello") });
    await waitFor(() => expect(screen.queryByText("notes.txt")).toBeNull());
  });

  it("a file alone is enough to send, and Remove takes the chip away", async () => {
    const user = userEvent.setup();
    render(<Composer onSubmit={vi.fn()} />);
    await user.upload(fileInput(), new File(["x"], "a.png", { type: "image/png" }));
    await screen.findByText("a.png");
    await user.click(screen.getByRole("button", { name: "Remove" }));
    expect(screen.queryByText("a.png")).toBeNull();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:p");
  });

  it("a failed attachment blocks the send until it is removed", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    render(<Composer onSubmit={onSubmit} />);
    const big = new File(["x"], "huge.bin");
    Object.defineProperty(big, "size", { value: 6 * 1024 * 1024 });
    await user.upload(fileInput(), big);
    expect(await screen.findByText("huge.bin is over the 5 MB limit for one file.")).toBeDefined();
    await user.type(box(), "go{Enter}");
    expect(onSubmit).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Remove" }));
    await user.click(box());
    await user.keyboard("{Enter}");
    expect(onSubmit).toHaveBeenCalledWith({ text: "go", attachments: [] });
  });

  it("takes the server's ten files, and says plainly which ones were left off", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    const err = vi.spyOn(toast, "error").mockImplementation((() => "") as never);
    render(<Composer onSubmit={onSubmit} />);
    const files = Array.from({ length: 12 }, (_, i) => new File(["x"], `f${i}.txt`));
    await user.upload(fileInput(), files);
    await screen.findByText("f9.txt");
    expect(screen.queryByText("f10.txt")).toBeNull();
    expect(err).toHaveBeenCalledWith(
      "A message takes up to 10 files. Not attached: f10.txt, f11.txt.",
    );
    // a file past the limit, one at a time, is refused the same way
    await user.upload(fileInput(), new File(["x"], "late.txt"));
    expect(screen.queryByText("late.txt")).toBeNull();
    expect(err).toHaveBeenLastCalledWith("A message takes up to 10 files. Not attached: late.txt.");
    await user.type(box(), "go{Enter}");
    expect(onSubmit.mock.calls[0][0].attachments).toHaveLength(10);
  });

  it("the steer box has no attach menu", () => {
    render(<Composer onSubmit={vi.fn()} allowAttachments={false} />);
    expect(screen.queryByRole("button", { name: "Add files or capture" })).toBeNull();
    expect(fileInput()).toBeNull();
  });
});

function dropEvent(type: string, file?: File) {
  const e = new Event(type, { bubbles: true, cancelable: true }) as Event & {
    dataTransfer: unknown;
  };
  e.dataTransfer = { types: ["Files"], files: file ? [file] : [], dropEffect: "" };
  return e;
}

describe("Composer window drop zone", () => {
  it("shows the overlay while a file is dragged, and a drop attaches it", async () => {
    render(<Composer onSubmit={vi.fn()} />);
    act(() => {
      window.dispatchEvent(dropEvent("dragenter"));
    });
    expect(screen.getByText("Drop files to attach")).toBeDefined();
    act(() => {
      window.dispatchEvent(
        dropEvent("drop", new File(["z"], "dropped.txt", { type: "text/plain" })),
      );
    });
    expect(await screen.findByText("dropped.txt")).toBeDefined();
    expect(screen.queryByText("Drop files to attach")).toBeNull();
  });

  it("leaving the window hides the overlay again", () => {
    render(<Composer onSubmit={vi.fn()} />);
    act(() => {
      window.dispatchEvent(dropEvent("dragenter"));
    });
    act(() => {
      window.dispatchEvent(dropEvent("dragleave"));
    });
    expect(screen.queryByText("Drop files to attach")).toBeNull();
  });

  it("a text-only box refuses a drop and attaches nothing", () => {
    render(<Composer onSubmit={vi.fn()} allowAttachments={false} />);
    const drop = dropEvent("drop", new File(["z"], "no.txt"));
    act(() => {
      window.dispatchEvent(drop);
    });
    expect(drop.defaultPrevented).toBe(true);
    expect(screen.queryByText("no.txt")).toBeNull();
  });
});

describe("Composer dictation and busy states", () => {
  it("the mic appears when speech is supported and its words land in the box", async () => {
    const user = userEvent.setup();
    let rec: { onresult: (e: unknown) => void } | undefined;
    class Fake {
      start = vi.fn();
      stop = vi.fn();
      onresult: ((e: unknown) => void) | null = null;
      constructor() {
        rec = this as never;
      }
    }
    (window as unknown as Record<string, unknown>).webkitSpeechRecognition = Fake;
    render(<Composer onSubmit={vi.fn()} />);
    await user.type(box(), "Dear team");
    await user.click(await screen.findByRole("button", { name: /Dictate/ }));
    act(() => rec?.onresult({ resultIndex: 0, results: [[{ transcript: "hello" }]] }));
    expect(box().value).toBe("Dear team hello");
  });

  it("while submitting the send button is disabled", () => {
    render(<Composer onSubmit={vi.fn()} status="submitting" />);
    const send = screen.getAllByRole("button").find((b) => b.getAttribute("type") === "submit");
    expect(send?.hasAttribute("disabled")).toBe(true);
  });

  it("with settings the box takes the landing layout and still sends", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    render(<Composer onSubmit={onSubmit} settings={<span>pills</span>} tall />);
    expect(screen.getByText("pills")).toBeDefined();
    await user.type(box(), "go{Enter}");
    expect(onSubmit).toHaveBeenCalledWith({ text: "go", attachments: [] });
  });
});

describe("Composer camera", () => {
  it("opens the photo dialog, says so when the camera is unavailable, and cancels", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("navigator", {
      ...navigator,
      mediaDevices: { getUserMedia: () => Promise.reject(new Error("denied")) },
    });
    render(<Composer onSubmit={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: "Add files or capture" }));
    await user.click(await screen.findByRole("menuitem", { name: "Take a photo" }));
    expect(await screen.findByText("Couldn't access the camera.")).toBeDefined();
    expect(screen.getByRole("button", { name: "Capture" }).hasAttribute("disabled")).toBe(true);
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByText("Take a photo")).toBeNull());
  });

  it("Capture screenshot reports a browser without screen capture", async () => {
    const user = userEvent.setup();
    const errorToast = vi.spyOn(toast, "error").mockImplementation((() => "") as never);
    vi.stubGlobal("navigator", { ...navigator, mediaDevices: {} });
    render(<Composer onSubmit={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: "Add files or capture" }));
    await user.click(await screen.findByRole("menuitem", { name: "Capture screenshot" }));
    expect(errorToast).toHaveBeenCalledWith("Screen capture isn't available in this browser.");
  });
});
