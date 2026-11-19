/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { expect, test, vi } from "vitest";

const { add } = vi.hoisted(() => ({ add: vi.fn((_options: unknown) => "toast-1") }));
vi.mock("@makeplane/propel/components/toast", () => ({
  createToastManager: () => ({ add }),
  ToastProvider: () => null,
}));

import { toast } from "./index";

test("each sonner-shaped call lands on the matching Propel variant", () => {
  toast("Saved draft");
  toast.success("Shared");
  toast.error("Could not save", { description: "Try again." });
  toast.info("Heads up");
  expect(add.mock.calls.map(([o]) => (o as { data: { variant: string } }).data.variant)).toEqual([
    "neutral",
    "success",
    "danger",
    "info",
  ]);
  expect(add.mock.calls[2]?.[0]).toMatchObject({ title: "Could not save", description: "Try again." });
});

test("an action becomes the toast's primary action", () => {
  const onClick = vi.fn();
  toast.success("Archived", { action: { label: "Undo", onClick } });
  expect(add.mock.lastCall?.[0]).toMatchObject({ data: { primaryAction: { label: "Undo", onClick } } });
});

test("duration is Base UI's timeout", () => {
  toast("Token copied", { duration: 15_000 });
  expect(add.mock.lastCall?.[0]).toMatchObject({ timeout: 15_000 });
});
