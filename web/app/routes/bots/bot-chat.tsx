/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The Chat tab - ONE THREAD with the bot (ADR-060). Talk comes back inline, streamed as it is
// written (the two-lane socket); real work appears as ONE CARD in the thread that fills
// in while it runs (its own event stream mounted while live), asks in place when it must, and
// ends holding the verified file. A reply typed under a card continues that work (the server's
// card anchor); the card's Reply is the explicit override. One composer, nothing
// beside it: the door's verdicts stay on the record and on Details, never in the chat.
//
// Threads: a one-chat bot IS its conversation; a many-chat bot shows the list
// on the left once a second chat exists.

import { Button } from "@makeplane/propel/components/button";
import { Icon } from "@makeplane/propel/components/icon";
import { TextArea } from "@makeplane/propel/components/text-area";
import { Check, Plus } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "@prismoid/blocks/toast";
import { ArtifactReader } from "~/components/artifact/artifact-reader";
import { Liveness, LoadingState, Markdown, Pill, UserBubble } from "~/components/kit";
import { useConversationStream } from "~/hooks/use-conversation-stream";
import { useLoad } from "~/hooks/use-load";
import { api, toastError, unwrap } from "~/lib/api/client";
import type { components } from "~/lib/api/types.gen";
import { type LiveReply, cardsOf, documentShaped, replyPending } from "~/lib/chat-stream";
import { ago } from "~/lib/format";
import { ArtifactChip, DetailsLink, WorkCard } from "./work-card";
import { healTail } from "~/lib/markdown-stream";
import { useSmoothText } from "~/lib/smooth-text";
import { cn } from "~/lib/utils";

// The card's Details link, re-exported for details-link.test.tsx.
export { DetailsLink };

type ConversationOut = components["schemas"]["ConversationOut"];
type MessageOut = components["schemas"]["MessageOut"];
type GateOut = components["schemas"]["GateOut"];

export function BotChat({ name, threadsMode = "many" }: { name: string; threadsMode?: string }) {
  const [threads, setThreads] = useState<ConversationOut[] | null>(null);
  const [current, setCurrent] = useState<string | null>(null);
  // The threads dial: a one-chat bot IS its conversation - no list, no New chat;
  // the first open lands in it (the server returns the one that exists).
  const oneChat = threadsMode === "one";

  const load = useCallback(async () => {
    const rows = await unwrap(api.GET("/v1/conversations", { params: { query: { bot: name } } }));
    setThreads(rows);
    setCurrent((c) => (c && rows.some((r) => r.id === c) ? c : (rows[0]?.id ?? null)));
  }, [name]);

  const open = useCallback(async () => {
    const row = await unwrap(api.POST("/v1/conversations", { body: { bot: name } }));
    setThreads((t) => (t?.some((r) => r.id === row.id) ? t : [row, ...(t ?? [])]));
    setCurrent(row.id);
  }, [name]);

  useEffect(() => {
    setThreads(null);
    setCurrent(null);
    void load()
      .then(async () => {
        if (oneChat) await open(); // a one-chat bot opens straight into its chat
      })
      .catch((e) => toastError(e, "Couldn't open the conversations."));
  }, [load, open, oneChat]);

  async function newChat() {
    try {
      await open();
    } catch (e) {
      toastError(e, "Couldn't start a new chat.");
    }
  }

  if (threads === null) return <LoadingState label="Opening the conversation…" />;

  // The list appears only once there is something to choose between.
  const list = !oneChat && threads.length >= 2;

  return (
    <div className="flex min-h-0 flex-1">
      {list && (
        <div className="flex w-60 shrink-0 flex-col gap-0.5 border-r px-2 py-3">
          <div className="flex items-center justify-between pr-1 pb-2 pl-2.5">
            <span className="text-muted-foreground text-caption-md-regular font-medium tracking-wide uppercase">
              Chats
            </span>
            <Button
              variant="ghost"
              size="xs"
              stretch="auto"
              onClick={() => void newChat()}
              aria-label="New chat"
              icon={<Icon icon={Plus} />}
              label="New"
            />
          </div>
          {threads.map((t) => (
            <button
              key={t.id}
              type="button"
              onClick={() => setCurrent(t.id)}
              className={cn(
                "flex items-center justify-between gap-2 rounded-md px-2.5 py-2 text-left transition-colors",
                t.id === current
                  ? "bg-muted text-foreground"
                  : "text-muted-foreground hover:bg-muted/60",
              )}
              title={t.title ?? "Untitled"}
            >
              <span className="flex min-w-0 items-center gap-2">
                <span className="truncate text-body-sm-regular">{t.title ?? "New chat"}</span>
                {t.unread > 0 && t.id !== current && (
                  <span className="bg-primary inline-block size-1.5 shrink-0 rounded-full" />
                )}
              </span>
              <span className="text-muted-foreground shrink-0 text-caption-md-regular">
                {t.last_message_at ? ago(t.last_message_at) : ""}
              </span>
            </button>
          ))}
        </div>
      )}
      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        {!oneChat && !list && current && (
          <div className="flex justify-end px-4 pt-2">
            <Button
              variant="ghost"
              size="xs"
              stretch="auto"
              onClick={() => void newChat()}
              aria-label="New chat"
              icon={<Icon icon={Plus} />}
              label="New chat"
            />
          </div>
        )}
        {current ? (
          <Thread
            key={current}
            name={name}
            conversationId={current}
            projectId={threads.find((t) => t.id === current)?.project_id ?? null}
            onTitled={() => void load()}
          />
        ) : (
          <EmptyThread name={name} onStart={newChat} />
        )}
      </div>
    </div>
  );
}

function EmptyThread({ name, onStart }: { name: string; onStart: () => Promise<void> }) {
  return (
    <div className="text-muted-foreground flex flex-1 flex-col items-center justify-center gap-3 px-6 text-center text-body-sm-regular">
      <p>No conversations with {name} yet.</p>
      <Button
        variant="primary"
        size="sm"
        stretch="auto"
        onClick={() => void onStart()}
        icon={<Icon icon={Plus} />}
        label="Start one"
      />
    </div>
  );
}

function Thread({
  name,
  conversationId,
  projectId,
  onTitled,
}: {
  name: string;
  conversationId: string;
  /** The conversation's Project, when it has one: its tasks live there (the Details link's URL). */
  projectId: string | null;
  onTitled: () => void;
}) {
  // The two-lane socket: durable rows replayed then followed, the reply streamed
  // as it is written. `refresh` reads the record past the cursor after a gesture (the fold
  // dedupes, so a row the socket also delivers costs nothing).
  const { state, connected, ready, refresh, foldRows } = useConversationStream(conversationId);
  const rows = state.rows;
  const live = state.live;
  const [gates, setGates] = useState<Record<string, GateOut>>({});
  const [anchor, setAnchor] = useState<{ id: string; label: string } | null>(null);
  const [openArtifact, setOpenArtifact] = useState<string | null>(null);
  const scroller = useRef<HTMLDivElement | null>(null);
  const titled = useRef(false);
  // The record was read at least once (an empty thread counts): the composer may show even
  // while the socket is still finding its way.
  const loaded = ready || connected || rows.length > 0;

  // A reply is owed and not yet on the record - the "is replying…" line until the first delta.
  const pending = useMemo(() => replyPending(state), [state]);
  // Every task's rows fold into ONE card, placed where the task started.
  const cards = useMemo(() => cardsOf(rows), [rows]);
  const openGates = useMemo(() => rows.filter((r) => r.kind === "gate" && r.gate_id), [rows]);

  useEffect(() => {
    if (!titled.current && rows.some((m) => m.role === "user")) {
      titled.current = true;
      onTitled();
    }
  }, [rows, onTitled]);

  // Gate cards read the live gate (open or not) from the one gates list - the same shape
  // the docks already take on the task page, so a question in chat IS the task's question.
  // A gate list that fails leaves the card as a link to the task.
  useLoad(
    openGates.length ? (signal) => unwrap(api.GET("/v1/gates", { signal })) : null,
    [openGates.length, rows.length],
    {
      onData: (list) => {
        const byId: Record<string, GateOut> = {};
        for (const g of list) byId[g.id] = g;
        setGates(byId);
      },
    },
  );

  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight });
  }, [rows.length, live?.text.length]);

  // The write, then the read-back OUTSIDE the write's try: a read that stumbles after a
  // successful send must never say "Couldn't send that" (the socket carries the row anyway).
  // A failed write rethrows so the composer keeps the draft (the shared composer's rule).
  async function send(text: string) {
    let out;
    try {
      out = await unwrap(
        api.POST("/v1/conversations/{conversation_id}/messages", {
          params: { path: { conversation_id: conversationId } },
          body: { text, start_task: false, reply_to_task: anchor?.id ?? null },
        }),
      );
    } catch (e) {
      toastError(e, "Couldn't send that.");
      throw e;
    }
    setAnchor(null);
    if (out.verdict === "steer") toast.success("Steered the running work.");
    void refresh().catch(() => undefined);
  }

  // The reversible mistake: a reply that wanted to be a file becomes one in a tap.
  async function save(seq: number) {
    try {
      await unwrap(
        api.POST("/v1/conversations/{conversation_id}/messages/{seq}/save", {
          params: { path: { conversation_id: conversationId, seq } },
          body: { format: "pdf" },
        }),
      );
    } catch (e) {
      toastError(e, "Couldn't save that as a file.");
      return;
    }
    void refresh().catch(() => undefined);
  }

  async function decide(seq: number, verb: "start" | "decline") {
    try {
      await unwrap(
        api.POST(
          verb === "start"
            ? "/v1/conversations/{conversation_id}/proposals/{seq}/start"
            : "/v1/conversations/{conversation_id}/proposals/{seq}/decline",
          { params: { path: { conversation_id: conversationId, seq } } },
        ),
      );
      foldRows((r) =>
        r.map((m) =>
          m.seq === seq && m.proposal
            ? {
                ...m,
                proposal: {
                  ...m.proposal,
                  status: verb === "start" ? "started" : "declined",
                },
              }
            : m,
        ),
      );
    } catch (e) {
      toastError(e, verb === "start" ? "Couldn't start that." : "Couldn't decline that.");
      return;
    }
    void refresh().catch(() => undefined);
  }

  if (!loaded) return <LoadingState label="Opening the conversation…" />;

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div
        ref={scroller}
        className="scrollbar-thin flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto px-6 py-5"
      >
        {rows.length === 0 && (
          <p className="text-muted-foreground m-auto max-w-sm text-center text-body-sm-regular">
            Say what you need.
          </p>
        )}
        {rows.map((m) => {
          if (m.role !== "system" || !m.task_id || !cards.firstSeq.has(m.task_id)) {
            return (
              <Row
                projectId={projectId}
                key={m.seq}
                name={name}
                m={m}
                onDecide={decide}
                onOpenArtifact={setOpenArtifact}
                onSave={save}
              />
            );
          }
          if (cards.firstSeq.get(m.task_id) !== m.seq) return null; // folded into the card above
          return (
            <WorkCard
              key={`card-${m.task_id}`}
              projectId={projectId}
              botSeq={cards.botSeq.get(m.task_id)}
              taskId={m.task_id}
              rows={cards.byTask.get(m.task_id) ?? []}
              name={name}
              gates={gates}
              onOpenArtifact={setOpenArtifact}
              onReply={(id, label) => setAnchor({ id, label })}
              onGateResolved={() => void refresh()}
            />
          );
        })}
        {live ? (
          <LiveBubble name={name} live={live} />
        ) : (
          pending && (
            <div className="text-muted-foreground flex items-center gap-2 text-caption-md-regular">
              <Liveness className="size-3.5" /> {name} is replying…
            </div>
          )
        )}
        {!connected && rows.length > 0 && (
          <div className="text-muted-foreground text-caption-md-regular">
            Reconnecting to the conversation…
          </div>
        )}
      </div>
      <Composer name={name} anchor={anchor} onClearAnchor={() => setAnchor(null)} onSend={send} />
      <ArtifactReader
        artifactId={openArtifact}
        onClose={() => setOpenArtifact(null)}
        onArtifactChanged={setOpenArtifact}
      />
    </div>
  );
}

/** The reply as it is written: the bot's name, the tools it is using as a quiet
 *  line, and its text revealed at reading pace. Retired by the durable row the moment it lands. */
function LiveBubble({ name, live }: { name: string; live: LiveReply }) {
  const shown = useSmoothText(live.text, !live.ended);
  return (
    <div className="flex max-w-[92%] flex-col gap-2">
      <div className="text-muted-foreground text-caption-md-regular">{name}</div>
      {live.tools.length > 0 && (
        <div className="text-muted-foreground flex flex-wrap items-center gap-3 text-caption-md-regular">
          {live.tools.map((t, i) => (
            <span key={i} className="flex items-center gap-1.5">
              {t.done ? <Check className="text-success size-3" /> : <Liveness className="size-3" />}
              {t.label || t.tool}
            </span>
          ))}
        </div>
      )}
      {shown.trim() ? (
        <Markdown className="text-body-sm-regular" breaks>
          {healTail(shown)}
        </Markdown>
      ) : (
        <div className="text-muted-foreground flex items-center gap-2 text-caption-md-regular">
          <Liveness className="size-3.5" /> {name} is replying…
        </div>
      )}
    </div>
  );
}

function Row({
  name,
  projectId,
  m,
  onDecide,
  onOpenArtifact,
  onSave,
}: {
  name: string;
  projectId: string | null;
  m: MessageOut;
  onDecide: (seq: number, verb: "start" | "decline") => Promise<void>;
  onOpenArtifact: (id: string) => void;
  onSave: (seq: number) => Promise<void>;
}) {
  if (m.role === "user") {
    return (
      <div className="flex flex-col items-end gap-1">
        <UserBubble>{m.text}</UserBubble>
        {m.verdict === "refused" && (
          <span className="text-destructive text-caption-md-regular">
            Refused: {m.verdict_reason}
          </span>
        )}
        {m.status === "failed" && m.verdict !== "refused" && (
          <span className="text-muted-foreground text-caption-md-regular">{m.detail}</span>
        )}
      </div>
    );
  }
  if (m.role === "bot") {
    return (
      <div className="flex max-w-[92%] flex-col gap-2">
        <div className="text-muted-foreground text-caption-md-regular">{name}</div>
        <Markdown className="text-body-sm-regular" breaks>
          {m.text}
        </Markdown>
        {m.artifact_ids.length > 0 && (
          <div className="flex flex-wrap items-center gap-1.5">
            {m.artifact_ids.map((id) => (
              <ArtifactChip key={id} id={id} onOpen={onOpenArtifact} />
            ))}
            {m.task_id && <DetailsLink task={{ id: m.task_id, project_id: projectId }} />}
          </div>
        )}
        {m.proposal && (
          <ProposalCard seq={m.seq} p={m.proposal} projectId={projectId} onDecide={onDecide} />
        )}
        {!m.task_id && !m.proposal && documentShaped(m.text) && (
          <div>
            <Button
              variant="ghost"
              size="xs"
              stretch="auto"
              onClick={() => void onSave(m.seq)}
              label="Save as file"
            />
          </div>
        )}
        {remembered(m).length > 0 && (
          <p className="text-muted-foreground text-caption-md-regular">
            Remembered: {remembered(m).join(" · ")}
          </p>
        )}
      </div>
    );
  }
  // a system line that belongs to no task: a reply that could not be written, a stall
  return <p className="text-muted-foreground text-caption-md-regular">{m.text}</p>;
}

function remembered(m: MessageOut): string[] {
  const r = (m.meta as Record<string, unknown>).remembered;
  return Array.isArray(r) ? r.map(String) : [];
}

function ProposalCard({
  seq,
  p,
  projectId,
  onDecide,
}: {
  seq: number;
  p: NonNullable<MessageOut["proposal"]>;
  projectId: string | null;
  onDecide: (seq: number, verb: "start" | "decline") => Promise<void>;
}) {
  const [busy, setBusy] = useState<null | "start" | "decline">(null);
  const act = async (verb: "start" | "decline") => {
    setBusy(verb);
    try {
      await onDecide(seq, verb);
    } finally {
      setBusy(null);
    }
  };
  return (
    <div className="bg-card space-y-2 rounded-lg border p-3 text-body-sm-regular">
      <div className="flex items-center gap-2">
        <span className="text-muted-foreground text-caption-md-regular">Proposed work</span>
        {p.deliverable && <Pill tone="muted">{p.deliverable}</Pill>}
        {p.status !== "pending" && (
          <Pill tone={p.status === "started" ? "success" : "muted"}>{p.status}</Pill>
        )}
      </div>
      <p>{p.goal}</p>
      {p.why && <p className="text-muted-foreground text-caption-md-regular">{p.why}</p>}
      {p.status === "pending" ? (
        <div className="flex gap-2">
          <Button
            variant="primary"
            size="sm"
            stretch="auto"
            disabled={busy !== null}
            onClick={() => void act("start")}
            label="Start"
          />
          <Button
            variant="ghost"
            size="sm"
            stretch="auto"
            disabled={busy !== null}
            onClick={() => void act("decline")}
            label="Not now"
          />
        </div>
      ) : p.status === "started" && p.task_id ? (
        <DetailsLink task={{ id: p.task_id, project_id: projectId }} />
      ) : null}
    </div>
  );
}

function Composer({
  name,
  anchor,
  onClearAnchor,
  onSend,
}: {
  name: string;
  anchor: { id: string; label: string } | null;
  onClearAnchor: () => void;
  onSend: (text: string) => Promise<void>;
}) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    const t = text.trim();
    if (!t || busy) return;
    setBusy(true);
    try {
      await onSend(t);
      setText(""); // cleared only once the send landed - a failed send keeps the draft
    } catch {
      /* the thread already toasted; the draft stays */
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="px-6 pb-4">
      {anchor && (
        <div className="text-muted-foreground mb-1.5 flex items-center gap-2 text-caption-md-regular">
          <span>Replying to: {anchor.label}</span>
          <button
            type="button"
            className="hover:text-foreground"
            onClick={onClearAnchor}
            aria-label="Clear reply target"
          >
            ✕
          </button>
        </div>
      )}
      <div className="bg-card flex items-end gap-2 rounded-lg p-2 shadow-[inset_0_0_0_1px_var(--input)]">
        <div className="flex min-h-9 flex-1 items-center px-2">
          <TextArea
            size="md"
            surface="inline"
            autoResize
            maxRows={10}
            value={text}
            onChange={(e) => setText(e.currentTarget.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void submit();
              }
            }}
            placeholder={anchor ? "Say what should change…" : `Message ${name}…`}
            rows={1}
            aria-label={`Message ${name}`}
          />
        </div>
        <button
          type="button"
          onClick={() => void submit()}
          disabled={busy || !text.trim()}
          aria-label="Send"
          className="bg-primary text-primary-foreground flex size-8 shrink-0 items-center justify-center rounded-full disabled:opacity-40"
        >
          <svg
            width="15"
            height="15"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <path d="m5 12 7-7 7 7" />
            <path d="M12 19V5" />
          </svg>
        </button>
      </div>
    </div>
  );
}
