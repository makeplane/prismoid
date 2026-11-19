/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The question dock - the task page's gate interface, docked directly above
// the composer the way modern ask bars stage their clarifying questions.
//
// The grammar (one card, one question at a time):
//   header   the QUESTION is the title · ‹ n of m › stepper · Skip (this question) ·
//            Use defaults (decline the rest: what you answered stands) · ✕ collapse
//   rows     numbered options - click answers and advances; the number is also what a
//            direct reply in the composer below may type ("2" picks row 2)
//   free row pencil + input + send, for text/number questions and `allow_other`
//
// Answering the LAST question submits the whole batch (accept + content); nothing is sent
// before that, so ‹ › can revisit and revise any draft. The composer below stays live the
// whole time ("Or reply directly…") - the route forwards its text here via the ref handle.
//
// The inbox's GateCard remains the all-questions-at-once sibling (it answers gates inside
// a notification pane, where a stepper would fight the pane's own scroll). One wire, two
// stages: both resolve through POST /gates/{id}/resolve.

import { Button } from "@makeplane/propel/components/button";
import { Icon } from "@makeplane/propel/components/icon";
import { IconButton } from "@makeplane/propel/components/icon-button";
import { ChevronLeft, ChevronRight, Pencil, ArrowUp, X } from "lucide-react";
import { forwardRef, useImperativeHandle, useState, type ForwardedRef } from "react";
import { toastError, api, unwrap } from "~/lib/api/client";
import type { Question } from "~/lib/api/events";
import type { GateView } from "~/lib/task-view";
import { cn } from "~/lib/utils";

/** The draft that belongs in the free-text input when arriving at `q`: its saved draft,
 *  unless that draft is one of its option VALUES (a click - the rows already show it). */
export function typedDraft(q: Question, drafts: Record<string, Draft>): string {
  const d = drafts[q.id];
  return typeof d === "string" && !optionRows(q).some((r) => r.value === d) ? d : "";
}

/** The drafts with one question's answer taken back: a skipped question sends nothing. */
function withoutDraft(drafts: Record<string, Draft>, id: string): Record<string, Draft> {
  return Object.fromEntries(Object.entries(drafts).filter(([key]) => key !== id));
}

/** One draft answer: option values (or free text) as the wire will send them. */
type Draft = string | string[];

export type QuestionDockHandle = {
  /** The composer's "Or reply directly…" lands here: a bare number picks that option row;
   *  anything else is the free-text answer. Advances (or submits, on the last question). */
  answerCurrent: (text: string) => void;
};

// ── the pure core (exported for tests) ───────────────────────────────────────

/** The rows a question renders: real options, or the Yes/No pair a boolean implies. */
export function optionRows(q: Question): { value: string; label: string }[] {
  if (q.type === "boolean")
    return [
      { value: "true", label: "Yes" },
      { value: "false", label: "No" },
    ];
  return (q.options ?? []).map((o) => ({ value: o.value, label: o.label }));
}

/** A direct reply that is a bare row number picks that row; anything else is free text. */
export function matchDirectReply(
  q: Question,
  text: string,
): { kind: "option"; value: string } | { kind: "text"; value: string } {
  const rows = optionRows(q);
  const n = /^\d{1,2}$/.test(text.trim()) ? Number(text.trim()) : NaN;
  if (n >= 1 && n <= rows.length) return { kind: "option", value: rows[n - 1].value };
  return { kind: "text", value: text.trim() };
}

/** One message, every answer: a direct reply shaped like "1: kvm; 2: yes" -
 *  segments split on newlines, semicolons, or an inline " 2." boundary - targets question
 *  N with each fragment. A fragment that is a bare option number picks that option; one
 *  that matches an option label (case-insensitive, unique) picks it too; anything else is
 *  the free-text answer. Returns null when the text isn't batch-shaped (fewer than two
 *  numbered segments), so the caller falls back to answering the CURRENT question. */
export function parseBatch(questions: Question[], text: string): Record<string, Draft> | null {
  const segments = text
    .split(/\n|;/)
    .flatMap((line) => line.split(/(?=(?:^|\s)\d{1,2}[.):]\s)/))
    .map((seg) => seg.trim())
    .filter(Boolean);
  const drafts: Record<string, Draft> = {};
  let hits = 0;
  for (const seg of segments) {
    const m = /^(\d{1,2})[.):]?\s+(.+)$/.exec(seg);
    if (!m) continue;
    const n = Number(m[1]);
    if (n < 1 || n > questions.length) continue;
    const q = questions[n - 1];
    drafts[q.id] = matchAnswer(q, m[2].trim());
    hits += 1;
  }
  return hits >= 2 ? drafts : null;
}

/** A fragment against one question: option number → that option; unique label match →
 *  that option; anything else stays free text. Multi-selects take comma-separated picks. */
export function matchAnswer(q: Question, fragment: string): Draft {
  const rows = optionRows(q);
  const pick = (piece: string): string => {
    const n = /^\d{1,2}$/.test(piece) ? Number(piece) : NaN;
    if (n >= 1 && n <= rows.length) return rows[n - 1].value;
    const hit = rows.filter((r) => r.label.toLowerCase().includes(piece.toLowerCase()));
    return hit.length === 1 ? hit[0].value : piece;
  };
  if (q.type === "multi_select")
    return fragment
      .split(",")
      .map((piece) => pick(piece.trim()))
      .filter(Boolean);
  return pick(fragment);
}

/** Drafts → the wire's `content` (values only; unanswered optional questions are omitted).
 *  Returns the ids of REQUIRED questions still unanswered instead, when any. */
export function buildContent(
  questions: Question[],
  drafts: Record<string, Draft>,
):
  | { ok: true; content: Record<string, string | number | boolean | string[]> }
  | { ok: false; missing: string[] } {
  const content: Record<string, string | number | boolean | string[]> = {};
  const missing: string[] = [];
  for (const q of questions) {
    const draft = drafts[q.id];
    const empty =
      draft === undefined || draft === "" || (Array.isArray(draft) && draft.length === 0);
    if (empty) {
      if (q.required) missing.push(q.id);
      continue;
    }
    if (q.type === "number") content[q.id] = Number(draft);
    else if (q.type === "boolean") content[q.id] = draft === "true";
    else content[q.id] = draft;
  }
  return missing.length ? { ok: false, missing } : { ok: true, content };
}

// ── the dock ─────────────────────────────────────────────────────────────────

type QuestionDockProps = {
  gate: GateView;
  /** Fired once the resolve lands, with WHO the server says resolved it - not always you: two
   *  people can be looking at one question, and the second gets the first one's resolution. */
  onResolved?: (resolvedBy?: string, resolvedById?: string | null, action?: string) => void;
  className?: string;
};

/** Total: a gate with no questions (a budget park, a cost confirmation, or an approve or
 *  connect gate a host mounted here by mistake) has nothing to step through and renders nothing -
 *  the stepper below indexes its questions and would take the whole page down. */
export const QuestionDock = forwardRef(function QuestionDock(
  props: QuestionDockProps,
  ref: ForwardedRef<QuestionDockHandle>,
) {
  return props.gate.questions.length > 0 ? <QuestionStepper ref={ref} {...props} /> : null;
});

const QuestionStepper = forwardRef(function QuestionStepper(
  { gate, onResolved, className }: QuestionDockProps,
  ref: ForwardedRef<QuestionDockHandle>,
) {
  const questions = gate.questions;
  const [idx, setIdx] = useState(0);
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const [freeText, setFreeText] = useState("");
  const [needed, setNeeded] = useState(false); // current question skipped while required
  const [busy, setBusy] = useState(false);
  const [collapsed, setCollapsed] = useState(false);

  const q = questions[idx];
  const rows = optionRows(q);
  const last = idx === questions.length - 1;
  // text/number questions ARE the free row; selects offer it only with allow_other.
  const freeRow = q.type === "text" || q.type === "number" || q.allow_other;

  async function resolve(action: "accept" | "decline", drafted: Record<string, Draft>) {
    if (busy) return;
    if (action === "accept") {
      const built = buildContent(questions, drafted);
      if (!built.ok) {
        // jump back to the first unanswered required question and say so
        setIdx(questions.findIndex((x) => x.id === built.missing[0]));
        setNeeded(true);
        return;
      }
      setBusy(true);
      try {
        const res = await unwrap(
          api.POST("/v1/gates/{gate_id}/resolve", {
            params: { path: { gate_id: gate.gate_id } },
            body: { action, content: built.content },
          }),
        );
        onResolved?.(res.resolved_by, res.resolved_by_id, res.action);
      } catch (e) {
        toastError(e, "Couldn't submit your answers.");
      } finally {
        // ALWAYS release the latch. On success the gate.resolved fold unmounts this dock -
        // but when several gates are stacked, the route's key-remount is the real reset and
        // a latch left true would arrive pre-disabled on the next gate (the
        // second question's Skip did nothing until a page refresh).
        setBusy(false);
      }
      return;
    }
    // Decline = "use the defaults for the rest": what was already answered rides along and
    // stands over its default (the server keeps it; it used to drop every typed answer).
    const given = buildContent(
      questions.map((x) => ({ ...x, required: false })),
      drafted,
    );
    const content = given.ok && Object.keys(given.content).length ? given.content : undefined;
    setBusy(true);
    try {
      const res = await unwrap(
        api.POST("/v1/gates/{gate_id}/resolve", {
          params: { path: { gate_id: gate.gate_id } },
          body: { action, ...(content ? { content } : {}) },
        }),
      );
      onResolved?.(res.resolved_by, res.resolved_by_id, res.action);
    } catch (e) {
      toastError(e, "Couldn't use the defaults.");
    } finally {
      setBusy(false);
    }
  }

  /** Record the current question's answer, then move on - or submit from the last one. */
  function go(to: number) {
    // ‹ › promise revisit-and-revise: an unsent typed answer is saved as the draft of the
    // question it was typed under (it silently vanished and visually leaked
    // into the next question), and the destination's typed draft comes back for revision.
    const cur = gate.questions[idx];
    const text = freeText.trim();
    const next = text ? { ...drafts, [cur.id]: text } : drafts;
    if (text) setDrafts(next);
    setFreeText(typedDraft(gate.questions[to], next));
    setIdx(to);
  }

  /** Skip THIS question: no answer (its default applies, if it has one) and on to the next;
   *  from the last one, the batch is submitted as answered so far. */
  function skip() {
    const next = withoutDraft(drafts, gate.questions[idx].id);
    setDrafts(next);
    setNeeded(false);
    if (last) {
      setFreeText("");
      void resolve("accept", next);
      return;
    }
    setFreeText(typedDraft(gate.questions[idx + 1], next));
    setIdx(idx + 1);
  }

  function answer(draft: Draft) {
    const next = { ...drafts, [gate.questions[idx].id]: draft };
    setDrafts(next);
    setNeeded(false);
    setFreeText("");
    if (last) void resolve("accept", next);
    else setIdx(idx + 1);
  }

  function toggleMulti(value: string) {
    const cur = drafts[q.id];
    const arr = Array.isArray(cur) ? cur : [];
    const next = arr.includes(value) ? arr.filter((v) => v !== value) : [...arr, value];
    setDrafts({ ...drafts, [q.id]: next });
    setNeeded(false);
  }

  useImperativeHandle(ref, () => ({
    answerCurrent: (text: string) => {
      if (!text.trim() || busy) return;
      // "1: kvm; 2: yes" answers them ALL in one send; anything else answers the
      // current question (a bare number still picks its option row).
      const batch = parseBatch(questions, text);
      if (batch) {
        const next = { ...drafts, ...batch };
        setDrafts(next);
        setNeeded(false);
        const built = buildContent(questions, next);
        if (built.ok) {
          void resolve("accept", next);
        } else {
          setIdx(questions.findIndex((x) => x.id === built.missing[0]));
          setNeeded(true);
        }
        return;
      }
      const match = matchDirectReply(gate.questions[idx], text);
      if (match.kind === "option" && gate.questions[idx].type === "multi_select") {
        toggleMulti(match.value);
        return;
      }
      answer(match.value);
    },
  }));

  if (collapsed) {
    return (
      <button
        type="button"
        onClick={() => setCollapsed(false)}
        className={cn(
          "bg-popover ring-foreground/10 shadow-(--shadow-overlay) hover:bg-muted/40 flex w-full items-center justify-between rounded-xl px-4 py-2.5 text-left ring-1 transition-colors",
          className,
        )}
      >
        <span className="text-body-sm-regular font-medium">
          {questions.length} question{questions.length === 1 ? "" : "s"} waiting
        </span>
        <span className="text-muted-foreground text-caption-md-regular">Answer</span>
      </button>
    );
  }

  const selected = drafts[q.id];
  return (
    <div
      className={cn(
        "bg-popover ring-foreground/10 rounded-xl p-2 shadow-(--shadow-overlay) ring-1",
        className,
      )}
    >
      {/* header: the question IS the title - shown WHOLE, never truncated (a
          clamped prompt read "…telecom/mobile industry experience …" and the user had to
          answer a question they could not read). The chrome top-aligns on the first line. */}
      <div className="flex items-start gap-2 px-2 pt-1.5 pb-2">
        <h3 className="min-w-0 flex-1 pt-1 text-body-sm-regular font-medium break-words">
          {q.prompt}
          {needed && <span className="text-destructive font-regular"> · this one's required</span>}
        </h3>
        {questions.length > 1 && (
          <span className="text-muted-foreground flex shrink-0 items-center gap-0.5 text-caption-md-regular tabular-nums">
            <IconButton
              variant="ghost"
              size="sm"
              aria-label="Previous question"
              disabled={idx === 0}
              onClick={() => go(idx - 1)}
              icon={<Icon icon={ChevronLeft} />}
            />
            {idx + 1} of {questions.length}
            <IconButton
              variant="ghost"
              size="sm"
              aria-label="Next question"
              disabled={last}
              onClick={() => go(idx + 1)}
              icon={<Icon icon={ChevronRight} />}
            />
          </span>
        )}
        <Button
          variant="ghost"
          size="sm"
          stretch="auto"
          title="Skip this question"
          disabled={busy}
          onClick={skip}
          label="Skip"
        />
        <Button
          variant="ghost"
          size="sm"
          stretch="auto"
          title="Answer the rest with their defaults; what you answered stands"
          disabled={busy}
          onClick={() => void resolve("decline", drafts)}
          label="Use defaults"
        />
        <IconButton
          variant="ghost"
          size="sm"
          aria-label="Hide the questions"
          onClick={() => setCollapsed(true)}
          icon={<Icon icon={X} />}
        />
      </div>

      {/* numbered option rows - the number a direct reply may type */}
      {rows.length > 0 && (
        <div className="space-y-0.5">
          {rows.map((row, i) => {
            const on =
              q.type === "multi_select"
                ? Array.isArray(selected) && selected.includes(row.value)
                : selected === row.value;
            return (
              <button
                key={row.value}
                type="button"
                disabled={busy}
                onClick={() =>
                  q.type === "multi_select" ? toggleMulti(row.value) : answer(row.value)
                }
                className={cn(
                  "hover:bg-muted/40 flex w-full items-center gap-3 rounded-lg px-2 py-2 text-left text-body-sm-regular transition-colors",
                  on && "bg-muted/60",
                )}
              >
                <span
                  className={cn(
                    "bg-muted text-muted-foreground flex size-7 shrink-0 items-center justify-center rounded-md text-caption-md-regular tabular-nums",
                    on && "text-foreground",
                  )}
                >
                  {i + 1}
                </span>
                {/* full label, wrapping - an option you can't read is one you can't pick */}
                <span className="min-w-0 flex-1 break-words">{row.label}</span>
              </button>
            );
          })}
        </div>
      )}

      {/* the free-text row: THE input for text/number questions, the "other" for selects,
          and multi-select's way to finish (send with it empty = done, advance) */}
      {(freeRow || q.type === "multi_select") && (
        <form
          className="mt-1 flex items-center gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            const text = freeText.trim();
            if (q.type === "multi_select" && !text) {
              answer(Array.isArray(selected) ? selected : []);
              return;
            }
            if (!text) return;
            answer(text);
          }}
        >
          <span className="bg-muted text-muted-foreground flex size-7 shrink-0 items-center justify-center rounded-md">
            <Pencil className="size-3.5" />
          </span>
          <input
            value={freeText}
            onChange={(e) => {
              setFreeText(e.currentTarget.value);
              setNeeded(false);
            }}
            inputMode={q.type === "number" ? "decimal" : undefined}
            placeholder={
              q.type === "multi_select"
                ? "Add your own, or send to continue…"
                : q.type === "text" || q.type === "number"
                  ? "Type your answer…"
                  : "Or type your own…"
            }
            className="placeholder:text-muted-foreground min-w-0 flex-1 bg-transparent text-body-sm-regular outline-none"
          />
          <IconButton
            type="submit"
            variant="primary"
            size="sm"
            aria-label={last ? "Answer and submit" : "Answer and continue"}
            disabled={busy}
            icon={<Icon icon={ArrowUp} />}
          />
        </form>
      )}
    </div>
  );
});
