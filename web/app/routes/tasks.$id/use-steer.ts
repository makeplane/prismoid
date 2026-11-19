/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { type RefObject, useCallback, useEffect, useRef, useState } from "react";
import { toast } from "@prismoid/blocks/toast";
import type { ComposerApi } from "~/components/composer/composer";
import { ApiError, api, toastError, unwrap } from "~/lib/api/client";
import type { TaskView } from "~/lib/task-view";
import { uuidV4 } from "~/lib/uuid";
import type { ScrollApi } from "./scroll-bridge";
import type { StaleSteer } from "./stale-steer-strip";
import {
  NO_STAMP,
  refusedSteerSurface,
  restamp,
  stampForSend,
  type SteerStamp,
} from "./steer-stamp";

/**
 * A steer and its stamp (steer-stamp.ts holds the rules). The stamp is the plan epoch this
 * screen showed when typing BEGAN, and one command id per draft (a resend is idempotent;
 * edited words are a new one). A steer issued against an epoch the task has since left is a
 * 409 naming what was missed - the stale strip above the box. Refs, not state: the stamp is
 * read at send time and must not re-render the page per keystroke. `boxRef` mirrors the box
 * for callbacks that outlive a render.
 */
export function useSteer({
  taskId,
  steerEpoch,
  held,
  foldLocal,
  onText,
  scrollApi,
  composerApi,
}: {
  taskId: string;
  steerEpoch: number;
  /** A held fork: its first directive is its LAUNCH, and that one says so. */
  held: boolean;
  foldLocal: (fn: (v: TaskView) => TaskView) => void;
  /** Everything else the box's text feeds (the suggestion chip, the typing line). */
  onText: (text: string) => void;
  scrollApi: RefObject<ScrollApi | null>;
  composerApi: RefObject<ComposerApi | null>;
}) {
  const stampRef = useRef<SteerStamp>(NO_STAMP);
  const boxRef = useRef("");
  const steerEpochRef = useRef(steerEpoch);
  useEffect(() => {
    steerEpochRef.current = steerEpoch;
  }, [steerEpoch]);
  // The refused steer the strip offers - mirrored in a ref so the (stable) text observer can
  // tell when the box no longer holds those words.
  const [stale, setStaleState] = useState<StaleSteer | null>(null);
  const staleRef = useRef<StaleSteer | null>(null);
  const setStale = useCallback((next: StaleSteer | null) => {
    staleRef.current = next;
    setStaleState(next);
  }, []);
  // Stable: the composer re-fires its observer whenever this identity changes. It runs inside
  // the composer's effect, so its id mint is the guarded one (`uuidV4`): `crypto.randomUUID`
  // is absent on a plain-http origin and a throw here would take the page down.
  const onDraft = useCallback(
    (text: string) => {
      boxRef.current = text;
      onText(text);
      const refused = staleRef.current?.text ?? null;
      stampRef.current = restamp(stampRef.current, text, {
        refused,
        epoch: steerEpochRef.current,
        mint: uuidV4,
      });
      // the strip closes once the box no longer holds the refused words (an edit, an empty box)
      if (refused !== null && refused.trim() !== text.trim()) setStale(null);
    },
    [onText, setStale],
  );

  // It applies at the running step's next safe point, and its bubble says so - the
  // acknowledgement line under it is the receipt, not a toast. `epoch` overrides the typing
  // stamp (a deliberate resend against the plan as it stands). Throws on failure; the callers
  // own the error.
  async function sendSteer(directive: string, opts?: { epoch?: number }) {
    // The attempt is stamped before it leaves: a lost answer keeps the id for a resend of these
    // words, and words edited since mint another (`restamp`).
    const stamp = stampForSend(stampRef.current, directive, uuidV4);
    stampRef.current = stamp;
    const out = await unwrap(
      api.POST("/v1/tasks/{task_id}/steer", {
        params: { path: { task_id: taskId } },
        body: {
          directive,
          command_id: stamp.commandId ?? uuidV4(),
          issued_epoch: opts?.epoch ?? stamp.epoch ?? steerEpochRef.current,
        },
      }),
    );
    stampRef.current = NO_STAMP;
    setStale(null);
    // The epoch this steer opened is the next draft's floor, before its own frame arrives -
    // a quick second steer must not read as stale against the author's first.
    foldLocal((v) => (out.epoch > v.steerEpoch ? { ...v, steerEpoch: out.epoch } : v));
    if (held)
      toast("Branch launched", {
        description: "Planning starts now, shaped by your direction.",
      });
    // The directive's bubble lands at the tail on the next durable frame - go meet it.
    requestAnimationFrame(() => scrollApi.current?.toEnd());
  }

  // The follow-up race's loser: two people pressed send on a settled task and the row lock let
  // one win. The 409 names the winner; the loser's text is still in the box (a rejected send
  // keeps the draft) and one tap re-sends it as a steer into the new turn.
  function offerSteer(message: string, text: string) {
    toast(message, {
      duration: 15_000,
      action: {
        label: "Send as steer",
        onClick: () => {
          // against the turn that won - the epoch as it stands at the click
          sendSteer(text, { epoch: steerEpochRef.current }).then(
            () => composerApi.current?.clear(text),
            (e: unknown) =>
              // the task moved again before the click: the strip, while the box still holds
              // these words - otherwise a toast, never nothing
              refusedSteerSurface(e, text, boxRef.current) === "strip" && e instanceof ApiError
                ? setStale({ text, missed: e.missed ?? [] })
                : toastError(e, "Couldn't send that steer."),
          );
        },
      },
    });
  }

  function discardStale() {
    if (stale) composerApi.current?.clear(stale.text);
    setStale(null);
    stampRef.current = NO_STAMP;
  }

  return { stale, setStale, onDraft, sendSteer, offerSteer, discardStale };
}

export type Steer = ReturnType<typeof useSteer>;
