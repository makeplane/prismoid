/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The Configure tab - the direct-editing surface, unchanged in spirit from the standalone
// page it used to be: kind facts as badges, the description and instructions as
// input boxes on the page, one dirty state, one "Save as draft vN+1"; the eval report says
// per case why the pill is the colour it is; versions below.

import { FlaskConical, Sparkles, Upload } from "lucide-react";
import { useEffect, useState } from "react";
import { useRevalidator } from "react-router";
import { toast } from "@prismoid/blocks/toast";
import { Pill, Rows, Section } from "~/components/kit";
import { Badge } from "@makeplane/propel/components/badge";
import { Button } from "@makeplane/propel/components/button";
import { Icon } from "@makeplane/propel/components/icon";
import { Input, InputGroup } from "@makeplane/propel/components/input";
import { api, toastError, unwrap } from "~/lib/api/client";
import { ago } from "~/lib/format";
import { cn } from "~/lib/utils";
import type { BotHead } from "./route";
import { Editor } from "./bot-editor";
import { ShareTemplate } from "./share-template";

type CaseRow = { goal: string; expected: string; chosen: string; ok: boolean };

const STATUS_TONE = {
  published: "success",
  draft: "warning",
  archived: "muted",
} as const;
const EVAL_TONE = {
  green: "success",
  red: "danger",
  untested: "muted",
} as const;

export function ConfigureTab({
  head,
  newest,
  draftAhead,
  onChanged,
}: BotHead & { onChanged: () => void }) {
  const revalidator = useRevalidator();
  const working = draftAhead ?? head;
  const report = working.eval_report as {
    passed?: number;
    total?: number;
    cases?: CaseRow[];
  };
  void newest;

  async function publish() {
    try {
      await unwrap(
        api.POST("/v1/bots/{name}/publish", {
          params: { path: { name: head.name } },
        }),
      );
      toast.success(`Published v${working.version}.`);
      revalidator.revalidate();
      onChanged();
    } catch (e) {
      toastError(e, "Couldn't publish."); // the 409 carries the whole story - shown verbatim
    }
  }

  // The authoring jobs: both run on the worker and land back on this row, so the
  // gesture here is start-then-poll - a few quiet revalidations, never a spinner takeover.
  // ONE interval, cleared on unmount and restarted (not stacked) by a second click; one
  // revalidation per tick (`onChanged` is the same revalidator one level up).
  const [pollingUntil, setPollingUntil] = useState(0);
  function pollResult() {
    setPollingUntil(Date.now() + 40_000);
  }
  useEffect(() => {
    if (pollingUntil <= Date.now()) return;
    const timer = window.setInterval(() => {
      if (Date.now() >= pollingUntil) {
        setPollingUntil(0);
        return;
      }
      revalidator.revalidate();
    }, 5000);
    return () => window.clearInterval(timer);
  }, [pollingUntil, revalidator]);

  async function draftForMe() {
    try {
      await unwrap(
        api.POST("/v1/bots/{name}/draft", {
          params: {
            path: { name: head.name },
            query: { project_id: working.project_id ?? undefined },
          },
        }),
      );
      toast.success("Writing a fuller draft - it lands here in a moment.");
      pollResult();
    } catch (e) {
      toastError(e, "Couldn't start the drafter.");
    }
  }

  async function testEvals() {
    try {
      await unwrap(
        api.POST("/v1/bots/{name}/test", {
          params: {
            path: { name: head.name },
            query: { project_id: working.project_id ?? undefined },
          },
        }),
      );
      toast.success("Running the selection evals - results land here in a moment.");
      pollResult();
    } catch (e) {
      toastError(e, "Couldn't start the evals.");
    }
  }

  return (
    <div className="scrollbar-thin min-h-0 flex-1 space-y-6 overflow-y-auto px-6 py-5">
      <div className="flex flex-wrap items-center gap-1.5">
        <Badge variant="neutral" size="xs" label={working.capability} />
        <Badge variant="neutral" size="xs" label={`publishes ${working.produces}`} />
        {working.tools.map((t) => (
          <Badge key={t} variant="grey" size="xs" label={t} />
        ))}
        {working.skills.map((s) => (
          <Badge key={s} variant="grey" size="xs" label={`skill · ${s}`} />
        ))}
        {working.connectors.map((c) => (
          <Badge key={c} variant="grey" size="xs" label={`connector · ${c}`} />
        ))}
        <span className="ml-auto flex items-center gap-1.5">
          {working.status === "draft" && (
            <Button
              variant="ghost"
              size="sm"
              stretch="auto"
              icon={<Icon icon={Sparkles} />}
              label="Draft for me"
              onClick={() => void draftForMe()}
            />
          )}
          {working.eval_status !== "green" && (
            <Button
              variant="secondary"
              size="sm"
              stretch="auto"
              icon={<Icon icon={FlaskConical} />}
              label="Test evals"
              onClick={() => void testEvals()}
            />
          )}
          {/* Read off the version being edited, not `draftAhead`: a bot that was never
              published has no published version for its draft to be ahead of. */}
          {working.status === "draft" && working.eval_status === "green" && (
            <Button
              variant="primary"
              size="sm"
              stretch="auto"
              icon={<Icon icon={Upload} />}
              label={`Publish v${working.version}`}
              onClick={() => void publish()}
            />
          )}
        </span>
      </div>

      <TitleRow
        name={head.name}
        projectId={working.project_id}
        title={working.title}
        onSaved={onChanged}
      />
      <WorkRow
        name={head.name}
        projectId={working.project_id}
        workMode={working.work_mode}
        budget={working.task_budget_usd}
        threads={working.threads}
        onSaved={onChanged}
      />

      <Editor
        key={`${working.name}@${working.version}`}
        bot={working}
        onSaved={() => {
          revalidator.revalidate();
          onChanged();
        }}
      />

      {(report.cases?.length ?? 0) > 0 && (
        <Section
          title="Selection evals"
          actions={
            <span className="text-muted-foreground text-caption-md-regular tabular-nums">
              {report.passed}/{report.total}
            </span>
          }
        >
          <Rows as="ul">
            {(report.cases ?? []).map((c, i) => (
              <li
                key={i}
                className="flex items-center gap-3 px-4 py-3 text-body-sm-regular"
                title={`expected ${c.expected} · the selector chose ${c.chosen || "none"}`}
              >
                <span className={c.ok ? "text-success" : "text-destructive"} aria-hidden>
                  {c.ok ? "✓" : "✗"}
                </span>
                <span className="min-w-0 flex-1 truncate">{c.goal}</span>
                <span className="text-muted-foreground shrink-0 text-caption-md-regular">
                  → {c.chosen || "none"}
                  {!c.ok && ` (want ${c.expected})`}
                </span>
              </li>
            ))}
          </Rows>
        </Section>
      )}

      {head.status === "published" && <ShareTemplate head={head} />}

      <Section title="Versions">
        <Rows as="ul">
          {/* the loader already holds every version via the messenger's list; show the head +
              draft succinctly - the full history stays one CLI away and uncontested */}
          {[draftAhead, head].filter(Boolean).map((v) => (
            <li key={v!.version} className="flex items-center gap-3 px-4 py-3 text-body-sm-regular">
              <span className="tabular-nums">v{v!.version}</span>
              <Pill
                tone={STATUS_TONE[v!.status as keyof typeof STATUS_TONE] ?? "muted"}
                className="capitalize"
              >
                {v!.status}
              </Pill>
              <Pill tone={EVAL_TONE[v!.eval_status as keyof typeof EVAL_TONE] ?? "muted"}>
                {v!.eval_status}
              </Pill>
              <span className="text-muted-foreground ml-auto text-caption-md-regular">
                {ago(v!.created_at)}
              </span>
            </li>
          ))}
        </Rows>
      </Section>
    </div>
  );
}

/** The title - a face like the icon: per name, PATCHed in place, never a new
 *  version, never the routing surface. */
function TitleRow({
  name,
  projectId,
  title,
  onSaved,
}: {
  name: string;
  projectId: string | null;
  title: string | null;
  onSaved: () => void;
}) {
  const [value, setValue] = useState(title ?? "");
  const dirty = value.trim() !== (title ?? "");

  async function save() {
    try {
      await unwrap(
        api.PATCH("/v1/bots/{name}", {
          params: { path: { name }, query: { project_id: projectId ?? undefined } },
          body: value.trim()
            ? {
                title: value.trim(),
                clear_icon: false,
                clear_hue: false,
                clear_title: false,
              }
            : { clear_title: true, clear_icon: false, clear_hue: false },
        }),
      );
      toast.success("Title saved.");
      onSaved();
    } catch (e) {
      toastError(e, "Couldn't save the title.");
    }
  }

  return (
    <Section
      title="Title"
      actions={
        dirty ? (
          <Button
            variant="primary"
            size="sm"
            stretch="auto"
            label="Save"
            onClick={() => void save()}
          />
        ) : undefined
      }
    >
      <Rows as="div">
        <div className="space-y-2 px-4 py-3.5">
          <p className="text-muted-foreground text-caption-md-regular">
            A human name for the page header - never the routing surface, which stays the
            description below.
          </p>
          <InputGroup size="lg">
            <Input
              size="lg"
              value={value}
              onChange={(e) => setValue(e.currentTarget.value)}
              placeholder="Contracts desk"
              maxLength={120}
              aria-label="Bot title"
            />
          </InputGroup>
        </div>
      </Rows>
    </Section>
  );
}

/** The work dials and the threads dial - per name like the title:
 *  may the bot start work from a conversation by itself, what one chat-born task may cost, and
 *  whether the bot holds one chat or a chat per topic. Talk is never work. */
function WorkRow({
  name,
  projectId,
  workMode,
  budget,
  threads,
  onSaved,
}: {
  name: string;
  projectId: string | null;
  workMode: string;
  budget: string;
  threads: string;
  onSaved: () => void;
}) {
  const [mode, setMode] = useState(workMode);
  const [cap, setCap] = useState(budget);
  const [threadsMode, setThreadsMode] = useState(threads);
  // Numbers compare as numbers: the server renders the cap as "5.00" and the person typed "5".
  const capDirty = cap.trim() === "" || Number(cap) !== Number(budget);
  const dirty = mode !== workMode || capDirty || threadsMode !== threads;

  async function save() {
    const usd = Number(cap);
    if (cap.trim() === "" || !Number.isFinite(usd) || usd < 0) {
      toast.error("The cap is dollars per task - a number."); // a blank is not $0
      return;
    }
    try {
      await unwrap(
        api.PATCH("/v1/bots/{name}", {
          params: { path: { name }, query: { project_id: projectId ?? undefined } },
          body: {
            work_mode: mode,
            task_budget_usd: usd,
            threads: threadsMode,
            clear_icon: false,
            clear_hue: false,
            clear_title: false,
          },
        }),
      );
      setCap(usd.toFixed(2)); // what the server will echo back, so Save rests
      toast.success("Work dials saved.");
      onSaved();
    } catch (e) {
      toastError(e, "Couldn't save the work dials.");
    }
  }

  const MODES: { value: string; label: string; hint: string }[] = [
    {
      value: "auto",
      label: "Auto",
      hint: "starts the work it proposes, from your messages, inside the cap",
    },
    { value: "ask", label: "Ask", hint: "proposes work; you tap Start" },
    {
      value: "never",
      label: "Never",
      hint: "a pure talker - never starts a task on its own",
    },
  ];
  const THREADS: { value: string; label: string; hint: string }[] = [
    {
      value: "one",
      label: "One chat",
      hint: "the bot is its one conversation",
    },
    {
      value: "many",
      label: "Many chats",
      hint: "a chat per topic; the list appears once there are two",
    },
  ];

  return (
    <Section
      title="Work from chat"
      actions={
        dirty ? (
          <Button
            variant="primary"
            size="sm"
            stretch="auto"
            label="Save"
            onClick={() => void save()}
          />
        ) : undefined
      }
    >
      <Rows as="div">
        <div className="space-y-3 px-4 py-3.5">
          <p className="text-muted-foreground text-caption-md-regular">
            A conversation never runs anything by accident. On Auto (the default) the bot starts the
            work it proposes from your own messages, inside the cap; on Ask it proposes and you tap
            Start. Strangers, over-cap work and actions in a connected service always ask.
          </p>
          <div className="flex flex-wrap gap-2" role="radiogroup" aria-label="Work mode">
            {MODES.map((m) => (
              <button
                key={m.value}
                type="button"
                role="radio"
                aria-checked={mode === m.value}
                onClick={() => setMode(m.value)}
                className={cn(
                  "rounded-md border px-2.5 py-1.5 text-left text-caption-md-regular transition-colors",
                  mode === m.value
                    ? "border-foreground/40 bg-muted"
                    : "border-border hover:bg-muted/60",
                )}
                title={m.hint}
              >
                <span className="font-medium">{m.label}</span>
                <span className="text-muted-foreground"> · {m.hint}</span>
              </button>
            ))}
          </div>
          <div className="flex flex-wrap items-center gap-2" role="radiogroup" aria-label="Threads">
            <span className="text-muted-foreground text-caption-md-regular">Threads</span>
            {THREADS.map((t) => (
              <button
                key={t.value}
                type="button"
                role="radio"
                aria-checked={threadsMode === t.value}
                onClick={() => setThreadsMode(t.value)}
                className={cn(
                  "rounded-md border px-2.5 py-1.5 text-left text-caption-md-regular transition-colors",
                  threadsMode === t.value
                    ? "border-foreground/40 bg-muted"
                    : "border-border hover:bg-muted/60",
                )}
                title={t.hint}
              >
                <span className="font-medium">{t.label}</span>
                <span className="text-muted-foreground"> · {t.hint}</span>
              </button>
            ))}
          </div>
          <label className="flex items-center gap-2 text-caption-md-regular">
            <span className="text-muted-foreground">Cap per task</span>
            <span className="text-muted-foreground">$</span>
            <span className="w-24 tabular-nums">
              <InputGroup size="lg">
                <Input
                  size="lg"
                  value={cap}
                  onChange={(e) => setCap(e.currentTarget.value)}
                  inputMode="decimal"
                  aria-label="Cap per task in dollars"
                />
              </InputGroup>
            </span>
          </label>
        </div>
      </Rows>
    </Section>
  );
}
