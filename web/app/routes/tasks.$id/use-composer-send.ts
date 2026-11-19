/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { type RefObject, useCallback, useState } from "react";
import { useNavigate } from "react-router";
import { toast } from "@prismoid/blocks/toast";
import { toInputs, type Attachment } from "~/components/composer/capture";
import type { ComposerApi } from "~/components/composer/composer";
import type { QuestionDockHandle } from "~/components/tasks/question-dock";
import { ApiError, api, toastError, unwrap } from "~/lib/api/client";
import type { components } from "~/lib/api/types.gen";
import { inputsProblem } from "~/lib/input-limits";
import { taskHref } from "~/lib/links";
import { addOptimisticUser, removeOptimistic, type TaskView } from "~/lib/task-view";
import { uuidV4 } from "~/lib/uuid";
import type { ScrollApi } from "./scroll-bridge";
import { proposalKind } from "./task-page";
import type { FollowUpDials } from "./use-follow-up-dials";
import type { Steer } from "./use-steer";

type FollowUpIn = components["schemas"]["FollowUpIn"];
type FollowUpExtras = Omit<FollowUpIn, "goal" | "clarify" | "client_token">;

/**
 * What the box's send does. The composer is ALWAYS available: a settled thread's box CONTINUES
 * the same task (next turn, same page, no navigation), a live one steers it, and an open
 * question takes the text as its answer. One `sending` latch covers every send - the box, a
 * proposal's Start, and the stale strip's Send anyway.
 */
export function useComposerSend({
  task,
  live,
  questionOpen,
  steerEpoch,
  foldLocal,
  dials,
  steer,
  dockRef,
  scrollApi,
  composerApi,
}: {
  task: { id: string; project_id?: string | null };
  live: boolean;
  /** An open question: the box's text answers it instead of steering. */
  questionOpen: boolean;
  steerEpoch: number;
  foldLocal: (fn: (v: TaskView) => TaskView) => void;
  dials: FollowUpDials;
  steer: Steer;
  dockRef: RefObject<QuestionDockHandle | null>;
  scrollApi: RefObject<ScrollApi | null>;
  composerApi: RefObject<ComposerApi | null>;
}) {
  const navigate = useNavigate();
  const [sending, setSending] = useState(false);

  // Settled → the follow-up CONTINUES this task: same row, same URL, same stream. The bubble
  // paints now; the durable task.continued frame reconciles it by this gesture's token (one
  // token per SEND GESTURE - a replay returns the same turn), and the page keeps reading. The
  // new turn anchors to the TOP of the viewport the moment it paints - the person never
  // scrolls to find their own words (reconciliation keeps the id, so the anchor survives).
  // Throws after rolling the optimistic bubble back.
  const followUp = useCallback(
    async (goal: string, extras: FollowUpExtras): Promise<string> => {
      const token = uuidV4();
      foldLocal((v) => addOptimisticUser(v, goal, token));
      requestAnimationFrame(() => scrollApi.current?.toTurn(`opt-${token}`));
      try {
        const accepted = await unwrap(
          api.POST("/v1/tasks/{task_id}/follow_up", {
            params: { path: { task_id: task.id } },
            body: { goal, clarify: "auto", client_token: token, ...extras },
          }),
        );
        return accepted.task_id;
      } catch (e) {
        foldLocal((v) => removeOptimistic(v, token));
        throw e;
      }
    },
    [task.id, foldLocal, scrollApi],
  );

  async function sendFollowUp(value: string, attachments: Attachment[]) {
    const accepted = await followUp(value, {
      ...(attachments.length ? { inputs: toInputs(attachments) } : {}),
      ...dials.wireFields(),
    });
    if (accepted !== task.id) {
      // Transitional safety: a server still minting follow-up children - follow it rather
      // than paint a turn on the wrong page.
      navigate(taskHref({ id: accepted, project_id: task.project_id }));
    }
    // The dials pick what the NEXT turn adds - this turn has them now, so the fill clears with
    // the send instead of reading as thread DNA.
    dials.clearTurn();
  }

  function reportSendError(e: unknown, value: string, files: readonly string[]) {
    if (live && e instanceof ApiError && e.code === "stale_steer")
      // the task moved on while this was typed - the strip says what changed
      steer.setStale({ text: value, missed: e.missed ?? [] });
    else if (!live && e instanceof ApiError && e.status === 409) steer.offerSteer(e.message, value);
    else {
      // A 422 about the attached files reads as a sentence about the files, not "the request body is not valid".
      const problem =
        e instanceof ApiError && e.status === 422 ? inputsProblem(e.fields, files) : undefined;
      if (problem) toast.error(problem);
      else toastError(e, "Couldn't send that.");
    }
  }

  async function submit({ text, attachments }: { text: string; attachments: Attachment[] }) {
    const value = text.trim();
    if (!value || sending) return;
    if (questionOpen) {
      // "Or reply directly…": the box's text answers the CURRENT question in the dock - a bare
      // row number picks that option (the CLI's numbered-answer grammar). An APPROVE gate
      // takes no text (its verbs are the two buttons) - typed words fall through to the steer
      // below instead of vanishing into a null ref.
      dockRef.current?.answerCurrent(value);
      return;
    }
    setSending(true);
    try {
      // Running/waiting → steer; it merges at the next boundary and shows as your turn.
      if (live) await steer.sendSteer(value);
      else await sendFollowUp(value, attachments);
    } catch (e) {
      reportSendError(
        e,
        value,
        attachments.filter((a) => a.state === "ready").map((a) => a.filename),
      );
      // Reject so the composer keeps the draft (text + files) for a retry - it clears only on
      // a resolved send. The optimistic bubble was already rolled back.
      throw e;
    } finally {
      setSending(false);
    }
  }

  /** Start a proposed task (the answerer's soft hatch): a follow-up with the deliverable dial
   *  set - the same door the box uses, one gesture, one token. Stable across renders: FeedRow
   *  is memoised, and a fresh function per streamed frame re-rendered every row - Markdown
   *  included - on every animation frame. */
  const startProposal = useCallback(
    async (goal: string, deliverable: string | null) => {
      const value = goal.trim();
      if (!value || sending) return;
      if (live) {
        toast.info("Wait for this turn to finish, then Start.");
        return;
      }
      setSending(true);
      const kind = proposalKind(deliverable);
      try {
        await followUp(value, kind ? { deliverable: kind } : {});
      } catch (e) {
        toastError(e, "Couldn't start that.");
      } finally {
        setSending(false);
      }
    },
    [followUp, live, sending],
  );

  // The stale strip's way on: send the same words against the plan as it stands now. The box
  // clears only while it still holds those words.
  async function sendAnyway() {
    const stale = steer.stale;
    if (!stale || sending) return;
    setSending(true);
    try {
      await steer.sendSteer(stale.text, { epoch: steerEpoch });
      composerApi.current?.clear(stale.text);
    } catch (e) {
      if (e instanceof ApiError && e.code === "stale_steer")
        steer.setStale({ text: stale.text, missed: e.missed ?? [] });
      else toastError(e, "Couldn't send that steer.");
    } finally {
      setSending(false);
    }
  }

  return { sending, submit, startProposal, sendAnyway };
}
