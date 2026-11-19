/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useCallback, useState } from "react";
import { useNavigate } from "react-router";
import { api, toastError, unwrap } from "~/lib/api/client";
import { taskHref } from "~/lib/links";
import { declineProposal, type TaskView } from "~/lib/task-view";
import { uuidV4 } from "~/lib/uuid";
import type { Rating } from "./response-actions";

/**
 * The task's one-shot verbs, each a POST whose durable frame redraws the page: pause, resume
 * and cancel; fork; retry; the thumbs; retract a steer; put the Machine to sleep or reset it;
 * and decline a proposal (local only). The callbacks memoised rows hold are stable.
 */
export function useTaskActions({
  task,
  feedback,
  foldLocal,
  revalidate,
}: {
  task: { id: string; project_id?: string | null };
  feedback: TaskView["feedback"];
  foldLocal: (fn: (v: TaskView) => TaskView) => void;
  /** Re-read the task after a Machine verb (its chip reads the task detail). */
  revalidate: () => void;
}) {
  const navigate = useNavigate();
  const [retrying, setRetrying] = useState(false);
  const [forking, setForking] = useState(false);
  const path = { task_id: task.id };

  async function control(intent: "pause" | "resume" | "cancel") {
    try {
      if (intent === "pause")
        await unwrap(api.POST("/v1/tasks/{task_id}/pause", { params: { path } }));
      else if (intent === "resume")
        await unwrap(api.POST("/v1/tasks/{task_id}/resume", { params: { path } }));
      else await unwrap(api.POST("/v1/tasks/{task_id}/cancel", { params: { path } }));
    } catch (e) {
      toastError(e, "Action failed.");
    }
  }

  async function machineAction(verb: "sleep" | "reset") {
    const params = { path };
    try {
      if (verb === "sleep") await unwrap(api.POST("/v1/tasks/{task_id}/machine/sleep", { params }));
      else await unwrap(api.POST("/v1/tasks/{task_id}/machine/reset", { params }));
      revalidate();
    } catch (e) {
      toastError(
        e,
        verb === "sleep" ? "Could not put the Machine to sleep." : "Could not reset the Machine.",
      );
    }
  }

  // The thumbs: paint now, record durably, revert on failure - the replayed frame agrees.
  async function sendFeedback(rating: Rating) {
    const prev = feedback;
    foldLocal((v) => ({ ...v, feedback: rating }));
    try {
      await unwrap(
        api.POST("/v1/tasks/{task_id}/feedback", { params: { path }, body: { rating } }),
      );
    } catch (e) {
      foldLocal((v) => ({ ...v, feedback: prev }));
      toastError(e, "Couldn't record that.");
    }
  }

  // Retry = run the latest ask again as a new turn. The task.continued frame folds the
  // repeated ask into the thread; the page stays put, exactly like a follow-up.
  async function retry() {
    if (retrying) return;
    setRetrying(true);
    try {
      await unwrap(
        api.POST("/v1/tasks/{task_id}/retry", {
          params: { path },
          body: { client_token: uuidV4() },
        }),
      );
    } catch (e) {
      toastError(e, "Couldn't retry.");
    } finally {
      setRetrying(false);
    }
  }

  // FORK is the separate, explicit gesture - the only action that mints a new task. ONE click:
  // the branch wears "[Forked] <label>", inherits the whole conversation and the parent's
  // dials, and lands HELD - it sits with the composer until the first directive launches it.
  // Never a dialog, never a question of its own.
  async function fork() {
    if (forking) return;
    setForking(true);
    try {
      const child = await unwrap(
        api.POST("/v1/tasks/{task_id}/fork", {
          params: { path },
          body: { client_token: uuidV4() },
        }),
      );
      navigate(taskHref({ id: child.task_id, project_id: task.project_id }));
    } catch (e) {
      toastError(e, "Couldn't fork the task.");
    } finally {
      setForking(false);
    }
  }

  // Retract: the author takes back a steer that has not applied yet. The `steer.retracted`
  // frame redraws the bubble's line; a lost race (it applied first) is the server's 409, said
  // in its own words.
  const retractSteer = useCallback(
    async (commandId: string) => {
      try {
        await unwrap(
          api.POST("/v1/tasks/{task_id}/steers/{command_id}/retract", {
            params: { path: { task_id: task.id, command_id: commandId } },
          }),
        );
      } catch (e) {
        toastError(e, "Couldn't retract that steer.");
      }
    },
    [task.id],
  );
  const declineProposalRow = useCallback(
    (id: string) => foldLocal((v) => declineProposal(v, id)),
    [foldLocal],
  );

  return {
    control,
    machineAction,
    sendFeedback,
    retry,
    retrying,
    fork,
    retractSteer,
    declineProposalRow,
  };
}

export type TaskActions = ReturnType<typeof useTaskActions>;
