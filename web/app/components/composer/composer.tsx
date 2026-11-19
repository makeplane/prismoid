/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { ArrowUp, Camera, Mic, Monitor, Paperclip, Plus, Square, X } from "lucide-react";
import { useEffect, useRef, useState, type MutableRefObject, type ReactNode } from "react";
import { toast } from "@prismoid/blocks/toast";
import {
  Attachment as AttachmentChip,
  AttachmentAction,
  AttachmentActions,
  AttachmentContent,
  AttachmentDescription,
  AttachmentGroup,
  AttachmentMedia,
  AttachmentTitle,
} from "@prismoid/blocks/attachment";
import { Button } from "@makeplane/propel/components/button";
import {
  Dialog,
  DialogActions,
  DialogBody,
  DialogClose,
  DialogCloseGroup,
  DialogContent,
  DialogHeader,
  DialogHeading,
  DialogMain,
  DialogTitle,
} from "@makeplane/propel/components/dialog";
import { Icon } from "@makeplane/propel/components/icon";
import { IconButton } from "@makeplane/propel/components/icon-button";
import { Menu, MenuContent, MenuItem, MenuTrigger } from "@makeplane/propel/components/menu";
import {
  PromptInput,
  PromptInputButton,
  PromptInputSubmit,
  PromptInputTextarea,
  type ChatStatus,
} from "@prismoid/blocks/prompt-input";
import { formatBytes } from "~/lib/format";
import { admitFiles, overLimitMessage } from "~/lib/input-limits";
import { cn } from "~/lib/utils";
import {
  captureScreenshot,
  fileToAttachment,
  grabFrame,
  useHeardText,
  useVoiceDictation,
  refuseOverLimit,
  type Attachment,
} from "./capture";
import { appendHeard } from "./voice";
import { VoiceMeter } from "./voice-meter";

export type { Attachment };
export type ComposerStatus = "idle" | "submitting" | "streaming";

/** What a host may do to the box from outside (see `apiRef`). */
export interface ComposerApi {
  insert: (text: string) => void;
  /** Empty the box - but only while it still holds `ifText` (trimmed), so a draft the person
   *  has since edited is never thrown away. No argument clears unconditionally. The task
   *  page uses it when a rejected follow-up is re-sent as a steer from the toast. */
  clear: (ifText?: string) => void;
  /** Put the cursor in the box: Home calls it when New task lands on a Home already open. */
  focus: () => void;
}

/** The mic, and while dictation is on a stop square with the meter beside it. Both wear the
 *  mic's own colour: recording is said by the moving bars, not by red. */
function DictateButton({ recording, onToggle }: { recording: boolean; onToggle: () => void }) {
  return (
    <>
      {recording && (
        <span role="status" className="text-secondary flex items-center pr-1">
          <VoiceMeter />
          <span className="sr-only">Listening…</span>
        </span>
      )}
      <PromptInputButton
        tooltip={recording ? "Stop dictation" : "Dictate"}
        onClick={() => onToggle()}
      >
        {recording ? <Square className="size-3 fill-current" /> : <Mic />}
      </PromptInputButton>
    </>
  );
}

/**
 * The one composer, used to open a conversation and to steer/answer/follow-up within it. Text is
 * controlled (so voice dictation can append into it), and capture is first-class: attach a file,
 * DROP files straight onto the box, grab a screenshot, snap a camera photo, or dictate. Whatever
 * you capture rides along as task `inputs`.
 */
export function Composer({
  onSubmit,
  onStop,
  status = "idle",
  placeholder = "Message…",
  autoFocus,
  className,
  allowAttachments = true,
  settings,
  tall,
  apiRef,
  onTextChange,
}: {
  onSubmit: (msg: { text: string; attachments: Attachment[] }) => void | Promise<void>;
  onStop?: () => void;
  status?: ComposerStatus;
  placeholder?: string;
  autoFocus?: boolean;
  className?: string;
  // File/screenshot/camera capture - off for steer/follow-up (the API takes only a directive/goal there;
  // task creation is the one call that accepts `inputs`). Voice→text stays on, since it's just text.
  allowAttachments?: boolean;
  /** Inline setting controls on the box's bottom row, left of capture/send - the landing
   *  composer puts mode/clarify/budget/project/skills there as pill+panel controls. Presence
   *  switches the box from the single-line chat layout to the landing layout. */
  settings?: ReactNode;
  /** Landing size: rest at ~3 lines instead of one. */
  tall?: boolean;
  /** Imperative insert handle (the ScrollBridge idiom): a host wires this so out-of-box
   *  gestures - "Use this draft" on a rendered email card - can stage text INTO
   *  the box for the user to review and send. Same append semantics as voice dictation;
   *  it never submits. */
  apiRef?: MutableRefObject<ComposerApi | null>;
  /** The draft, observed: the connector suggestion chip matches against what the
   *  user is typing. Fires on every change - typing, voice, insert, the post-send clear -
   *  so the host's derived state can never go stale. Additive; the box stays uncontrolled. */
  onTextChange?: (text: string) => void;
}) {
  const [text, setText] = useState("");
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [cameraOpen, setCameraOpen] = useState(false);
  // Drag-and-drop is capture too. Depth-counted: children fire enter/leave in pairs on
  // every crossing, so a plain boolean flickers off mid-hover.
  const [dragging, setDragging] = useState(false);
  const dragDepth = useRef(0);
  const fileRef = useRef<HTMLInputElement>(null);
  const boxRef = useRef<HTMLTextAreaElement>(null);
  const voice = useVoiceDictation(
    (t) => setText((cur) => appendHeard(cur, t)),
    (msg) => toast.error(msg),
  );
  useEffect(() => {
    if (!apiRef) return;
    apiRef.current = {
      insert: (t) => setText((cur) => (cur.trim() ? cur.trimEnd() + "\n\n" : "") + t),
      clear: (ifText) =>
        setText((cur) => (ifText === undefined || cur.trim() === ifText.trim() ? "" : cur)),
      focus: () => boxRef.current?.focus(),
    };
    return () => {
      apiRef.current = null;
    };
  }, [apiRef]);
  useEffect(() => {
    onTextChange?.(text);
  }, [text, onTextChange]);

  const busy = status !== "idle";
  const heard = useHeardText(text, setText, voice);
  const canSend = heard.shown.trim().length > 0 || attachments.length > 0;
  const chatStatus: ChatStatus | undefined =
    status === "submitting" ? "submitted" : status === "streaming" ? "streaming" : undefined;

  // A file the server would refuse never reaches the chips: only ready ones take a slot.
  const held = attachments.filter((a) => a.state === "ready").length;
  function push(att: Attachment) {
    if (att.state === "error") toast.error(att.error ?? "Couldn't attach that.");
    setAttachments((cur) => [...cur, att]);
  }
  function add(att: Attachment) {
    const refusal = refuseOverLimit(att, held);
    if (refusal) toast.error(refusal);
    else push(att);
  }
  async function addFiles(files: FileList | null) {
    const { fit, over } = admitFiles(held, Array.from(files ?? []));
    if (over.length > 0) toast.error(overLimitMessage(over.map((f) => f.name)));
    for (const f of fit) push(await fileToAttachment(f));
  }
  async function screenshot() {
    try {
      add(await captureScreenshot());
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Screen capture failed.");
    }
  }
  function remove(id: string) {
    setAttachments((cur) => {
      const a = cur.find((x) => x.id === id);
      if (a?.previewUrl) URL.revokeObjectURL(a.previewUrl);
      return cur.filter((x) => x.id !== id);
    });
  }

  // The drop zone is the WINDOW, not the box: nobody should have to hit a 48px target with
  // a file, and a missed drop must never let the browser navigate away to it. Handlers live
  // on window for the composer's lifetime; the overlay is the affordance. addFiles rides a
  // ref so the effect binds once per capability.
  const addFilesRef = useRef(addFiles);
  addFilesRef.current = addFiles;
  useEffect(() => {
    const hasFiles = (e: DragEvent) => e.dataTransfer?.types.includes("Files") ?? false;
    const enter = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      dragDepth.current += 1;
      if (allowAttachments) setDragging(true);
    };
    const over = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault(); // without this the browser navigates to the dropped file
      if (e.dataTransfer) e.dataTransfer.dropEffect = allowAttachments ? "copy" : "none";
    };
    const leave = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      dragDepth.current = Math.max(0, dragDepth.current - 1);
      if (dragDepth.current === 0) setDragging(false);
    };
    const drop = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      dragDepth.current = 0;
      setDragging(false);
      if (!allowAttachments) {
        // the steer box: directive-only by API - say so instead of silently eating the drop
        toast.error("This box takes only text - attach files on a follow-up or a new task.");
        return;
      }
      void addFilesRef.current(e.dataTransfer?.files ?? null);
    };
    window.addEventListener("dragenter", enter);
    window.addEventListener("dragover", over);
    window.addEventListener("dragleave", leave);
    window.addEventListener("drop", drop);
    return () => {
      window.removeEventListener("dragenter", enter);
      window.removeEventListener("dragover", over);
      window.removeEventListener("dragleave", leave);
      window.removeEventListener("drop", drop);
    };
  }, [allowAttachments]);

  // The double-create guard must be SYNCHRONOUS. `busy` is a prop off the parent's state,
  // and state lags a render - a held Enter (the registry submits on every keydown repeat,
  // and ui/ stays byte-identical) or a fast double-tap lands 2+ submits in that lag window,
  // which is exactly how one send minted two tasks. A ref flips in the same tick.
  const inFlight = useRef(false);
  function send({ text: submitted }: { text: string }) {
    const value = submitted.trim();
    if ((!value && attachments.length === 0) || busy || inFlight.current) return;
    if (attachments.some((a) => a.state === "error")) {
      toast.error("Remove the failed attachment first.");
      return;
    }
    inFlight.current = true;
    heard.keep();
    // Clear the draft ONLY once the send RESOLVES - clearing synchronously lost the text
    // (and the attached files) on any failed send. A rejection keeps the draft
    // in place so the user can retry; the parent owns the error toast. Revoke the sent
    // previews here too - remove() revokes on manual removal, and a send is the other exit.
    const sent = attachments;
    Promise.resolve(onSubmit({ text: value, attachments: sent }))
      .then(() => {
        setText("");
        setAttachments([]);
        for (const a of sent) if (a.previewUrl) URL.revokeObjectURL(a.previewUrl);
        if (voice.recording) voice.toggle();
      })
      .catch(() => {
        /* send failed - leave the text and files so nothing typed is lost */
      })
      .finally(() => {
        inFlight.current = false;
      });
  }

  return (
    <div className={cn("space-y-2", className)}>
      {dragging && (
        /* The whole window is the target; this just says so. pointer-events-none: the drop
           itself lands wherever it lands - the WINDOW listener catches it. The dashed border
           is the one sanctioned use: a transient drop affordance, not container chrome. */
        <div className="bg-background/60 pointer-events-none fixed inset-0 z-50 grid place-items-center backdrop-blur-sm">
          <div className="border-ring bg-popover flex flex-col items-center gap-1.5 rounded-xl border-2 border-dashed px-10 py-8 shadow-(--shadow-modal)">
            <Paperclip className="text-muted-foreground size-5" />
            <p className="text-body-sm-regular font-medium">Drop files to attach</p>
            <p className="text-muted-foreground text-caption-md-regular">
              They ride along as the task's inputs
            </p>
          </div>
        </div>
      )}
      {/* Controls ride the text line (inline-end) in the chat layout; with `settings` the box
          becomes the landing composer - text on top, one bottom row: “+”, the setting pills,
          then mic and send on the right. */}
      <PromptInput onSubmit={send}>
        {/* Attachments live INSIDE the ring, stacked above the text line (the reference
            grammar every modern ask bar shares). A bare data-align div, not an
            InputGroupAddon - the addon's focus-the-input click handler would swallow the
            chips' remove buttons (same lesson as the settings row). */}
        {attachments.length > 0 && (
          <div data-align="block-start" className="w-full px-3 pt-3">
            <AttachmentGroup className="w-full">
              {attachments.map((a) => (
                <AttachmentChip key={a.id} state={a.state === "ready" ? "done" : "error"} size="sm">
                  <AttachmentMedia variant={a.previewUrl ? "image" : "icon"}>
                    {a.previewUrl ? <img src={a.previewUrl} alt="" /> : <Paperclip />}
                  </AttachmentMedia>
                  <AttachmentContent>
                    <AttachmentTitle>{a.filename}</AttachmentTitle>
                    <AttachmentDescription>{a.error ?? formatBytes(a.size)}</AttachmentDescription>
                  </AttachmentContent>
                  <AttachmentActions>
                    <AttachmentAction aria-label="Remove" onClick={() => remove(a.id)}>
                      <X />
                    </AttachmentAction>
                  </AttachmentActions>
                </AttachmentChip>
              ))}
            </AttachmentGroup>
          </div>
        )}
        <PromptInputTextarea
          ref={boxRef}
          value={heard.shown}
          onChange={(e) => heard.edit(e.currentTarget.value)}
          placeholder={placeholder}
          autoFocus={autoFocus}
          className={cn(
            tall
              ? "min-h-24 px-3 pt-3 pb-1 text-body-sm-regular md:text-body-sm-regular/relaxed"
              : "min-h-12 py-3",
          )}
        />
        {(() => {
          // Capture folds behind one “+” (the Claude pattern): three icon buttons become a
          // labelled menu, which is what lets the whole bar live on a single line.
          const addMenu = allowAttachments && (
            <>
              <input
                ref={fileRef}
                type="file"
                multiple
                hidden
                onChange={(e) => {
                  void addFiles(e.currentTarget.files);
                  e.currentTarget.value = "";
                }}
              />
              <Menu>
                <MenuTrigger render={<PromptInputButton aria-label="Add files or capture" />}>
                  <Plus />
                </MenuTrigger>
                <MenuContent align="start">
                  <MenuItem
                    icon={<Icon icon={Paperclip} />}
                    label="Attach files"
                    onClick={() => fileRef.current?.click()}
                  />
                  <MenuItem
                    icon={<Icon icon={Monitor} />}
                    label="Capture screenshot"
                    onClick={() => void screenshot()}
                  />
                  <MenuItem
                    icon={<Icon icon={Camera} />}
                    label="Take a photo"
                    onClick={() => setCameraOpen(true)}
                  />
                </MenuContent>
              </Menu>
            </>
          );
          const voiceAndSend = (
            <>
              {voice.supported && (
                <DictateButton recording={voice.recording} onToggle={voice.toggle} />
              )}
              {/* Stop and send COEXIST: steering a live task needs a visible send, so the
                  disc never turns into the stop (that hid the whole feature). Stop is its
                  own quiet control; the disc stays the ↑. */}
              {onStop && chatStatus === undefined && (
                <PromptInputButton
                  tooltip="Stop the task"
                  onClick={onStop}
                  className="text-muted-foreground hover:text-destructive"
                >
                  <Square className="size-3 fill-current" />
                </PromptInputButton>
              )}
              {/* Send is Plane's ↑ square (the control corner, as its AI chat input); the
                  stateful icons (spinner/stop/error) still come from the primitive when active. */}
              <PromptInputSubmit
                className="ml-0.5"
                status={chatStatus}
                onStop={onStop}
                disabled={status === "submitting" || (status === "idle" && !canSend)}
              >
                {chatStatus === undefined ? <ArrowUp className="size-4" /> : undefined}
              </PromptInputSubmit>
            </>
          );
          return settings ? (
            // A real toolbar, not an input adornment. InputGroupAddon exists to decorate a
            // text field - cursor-text across the row, muted-label styling, and a click
            // handler that focuses the group's first <input> (which here is the budget
            // field). All wrong for interactive controls. The bare data-align attribute
            // opts into the group's column layout; the row itself is ours: pl-1 is the
            // text column's px-3 minus a ghost pill's own px-2, so the “+” and every pill
            // LABEL sit on the text column, while pr-3 keeps the full inset between the
            // filled send button and the border. One line, no wrap: “+” · settings · mic ·
            // send.
            <div
              data-align="block-end"
              className="flex w-full min-w-0 items-center gap-1 pr-3 pb-2.5 pl-1"
            >
              {addMenu}
              {/* Only the settings scroll. The row stays ONE LINE at every width: on a narrow
                  viewport the dials scroll sideways between a fixed "+" and a fixed send
                  cluster. When the whole row scrolled, a phone pushed send off the right edge
                  (the 390px audit). Popovers portal, so the scroller never cages them. Each dial
                  keeps its own width (shrink-0): squeezed, they overlapped instead of scrolling.
                  gap-0.5 lets the full set (every dial on) fit the widest composer, so desktop
                  never scrolls; on a phone scroll-fade-x softens the edge that has more dials. */}
              <div className="scrollbar-thin scroll-fade-x flex min-w-0 flex-1 items-center gap-0.5 overflow-x-auto [&>*]:shrink-0">
                {settings}
              </div>
              <span className="flex shrink-0 items-center gap-0.5 pl-2">{voiceAndSend}</span>
            </div>
          ) : (
            // A bare data-align row, as the settings row above: Propel's InputGroup has no
            // addon part, and the kit addon's focus-the-first-<input> click only ever hit the
            // hidden file input here. pb-3 centers the 24px controls in the 48px single-line
            // rest (they still pin to the bottom as the textarea grows).
            <div
              role="group"
              data-align="inline-end"
              className="order-last flex items-center gap-0.5 self-end pt-2 pr-2 pb-3 has-[>button]:mr-[-0.275rem]"
            >
              {addMenu}
              {voiceAndSend}
            </div>
          );
        })()}
      </PromptInput>

      {cameraOpen && <CameraDialog onCapture={add} onClose={() => setCameraOpen(false)} />}
    </div>
  );
}

function CameraDialog({
  onCapture,
  onClose,
}: {
  onCapture: (a: Attachment) => void;
  onClose: () => void;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    navigator.mediaDevices
      .getUserMedia({ video: { facingMode: "user" } })
      .then((s) => {
        if (cancelled) {
          s.getTracks().forEach((t) => t.stop());
          return;
        }
        streamRef.current = s;
        if (videoRef.current) {
          videoRef.current.srcObject = s;
          void videoRef.current.play();
        }
      })
      .catch(() => setError("Couldn't access the camera."));
    return () => {
      cancelled = true;
      streamRef.current?.getTracks().forEach((t) => t.stop());
    };
  }, []);

  async function snap() {
    if (!streamRef.current) return;
    try {
      onCapture(await grabFrame(streamRef.current, `photo-${Date.now()}.png`));
      onClose();
    } catch {
      setError("Capture failed.");
    }
  }

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent size="xs">
        <DialogCloseGroup>
          <IconButton
            variant="ghost"
            size="xs"
            aria-label="Close"
            icon={<Icon icon={X} />}
            render={<DialogClose />}
          />
        </DialogCloseGroup>
        <DialogMain>
          <DialogHeader>
            <DialogHeading>
              <DialogTitle>Take a photo</DialogTitle>
            </DialogHeading>
          </DialogHeader>
          <DialogBody>
            {error ? (
              <p className="text-destructive text-body-sm-regular">{error}</p>
            ) : (
              <video
                ref={videoRef}
                className="bg-muted aspect-video w-full rounded-lg"
                muted
                playsInline
              />
            )}
          </DialogBody>
        </DialogMain>
        <DialogActions>
          <Button variant="secondary" size="md" stretch="auto" label="Cancel" onClick={onClose} />
          <Button
            variant="primary"
            size="md"
            stretch="auto"
            icon={<Icon icon={Camera} />}
            label="Capture"
            onClick={() => void snap()}
            disabled={!!error}
          />
        </DialogActions>
      </DialogContent>
    </Dialog>
  );
}
