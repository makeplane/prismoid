/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The console's bot vocabulary - the tone maps the list and the detail page both speak
// (route-local code is never imported across routes, so shared words live here).

import type { Tone } from "~/components/kit";

/** Lifecycle tones: published earns the green; draft and archived stay quiet - the label
 *  carries the difference, the tint only says "live for selection" or not. */
export const BOT_STATUS_TONE: Record<string, Tone> = {
  published: "success",
  draft: "muted",
  archived: "muted",
};

/** Eval verdict tones - the same words the product's bots screen speaks. */
export const BOT_EVAL_TONE: Record<string, Tone> = {
  green: "success",
  red: "danger",
  untested: "muted",
};
