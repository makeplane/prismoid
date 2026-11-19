/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The emailed verification code lives in the link's fragment, which no server or proxy ever sees.
// It is read once, kept in sessionStorage only across a sign-in detour, and forgotten after use.
import { STORAGE } from "~/lib/storage";

/** The code from the URL fragment, else the one kept across the sign-in detour; "" for neither. */
export function readCode(hash: string, store: Storage): string {
  const fromLink = hash.replace(/^#/, "").trim();
  if (fromLink) return fromLink;
  try {
    return store.getItem(STORAGE.verifyCode) ?? "";
  } catch {
    return "";
  }
}

export function keepCode(code: string, store: Storage): void {
  try {
    store.setItem(STORAGE.verifyCode, code);
  } catch {
    // storage blocked: the detour loses the code and the page asks for a new link
  }
}

export function forgetCode(store: Storage): void {
  try {
    store.removeItem(STORAGE.verifyCode);
  } catch {
    // nothing kept
  }
}
