/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// In-app text editing (ADR-033), renderer-owned like the collection editor: the shell stays
// fetch/boundary/floor-only; the markdown and text renderers wrap their read views in this
// frame. The affordance gates on `type === "document"` - a `.csv` dataset also renders via
// the text hint and must never grow an editor - plus a host that can follow a new head; the
// SERVER enforces rights and kind at save (no viewer-role field exists on the wire - a
// forbidden save keeps the draft and names the reason). Markdown edits as SOURCE, mono, no
// live preview; the draft lives in memory only (a persisted draft would resurrect stale
// text over newer revisions).
import { Button } from "@makeplane/propel/components/button";
import { Icon } from "@makeplane/propel/components/icon";
import { TextArea, TextAreaGroup } from "@makeplane/propel/components/text-area";
import { Pencil } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import type { components } from "~/lib/api/types.gen";
import { useReviseText } from "./use-revise-text";

type ArtifactOut = components["schemas"]["ArtifactOut"];

export function EditableText({
  artifact,
  text,
  onArtifactChanged,
  children,
}: {
  artifact: ArtifactOut;
  text?: string;
  onArtifactChanged?: (artifactId: string) => void;
  /** The read-mode view this frame wraps. */
  children: ReactNode;
}) {
  const editable = artifact.type === "document" && !!onArtifactChanged && text != null;
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [confirmDiscard, setConfirmDiscard] = useState(false);
  const { save, saving, conflict } = useReviseText(artifact, (id) => {
    setEditing(false);
    onArtifactChanged?.(id);
  });
  const dirty = editing && draft !== (text ?? "");

  // A different revision arrived under the frame (History click, restore, external save):
  // close the editor rather than carry a stale draft over another revision's text. Drafts
  // are in-memory by design - resurrecting one against the wrong base would save v4's
  // words over v1 with no warning.
  useEffect(() => {
    setEditing(false);
    setConfirmDiscard(false);
  }, [artifact.id]);

  // Leaving with unsaved words must cost a confirmation - the browser's own.
  useEffect(() => {
    if (!dirty) return;
    const warn = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = ""; // the legacy-Chromium half of the same ask (MDN's compat pair)
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  if (!editable) return <>{children}</>;

  if (!editing) {
    return (
      <div className="group/edit relative">
        {children}
        <span className="absolute top-2 right-2 inline-flex opacity-0 transition-opacity group-hover/edit:opacity-100 has-focus-visible:opacity-100">
          <Button
            variant="ghost"
            size="sm"
            stretch="auto"
            onClick={() => {
              setDraft(text ?? "");
              setConfirmDiscard(false);
              setEditing(true);
            }}
            icon={<Icon icon={Pencil} />}
            label="Edit"
          />
        </span>
      </div>
    );
  }

  return (
    <div className="ring-foreground/10 space-y-3 rounded-lg p-4 ring-1">
      {/* Mono source: the family inherits into the textarea. autoResize grows it with the text;
          rows is its floor (was min-h-[50vh]). */}
      <div className="font-mono">
        <TextAreaGroup resize="none">
          <TextArea
            size="xl"
            surface="field"
            autoResize
            rows={20}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            autoFocus
            spellCheck
          />
        </TextAreaGroup>
      </div>
      {conflict != null && (
        <p className="text-warning text-caption-md-regular">
          v{conflict} arrived while you were editing - your draft is untouched; Save again bases on
          v{conflict}.
        </p>
      )}
      <div className="flex items-center gap-2">
        <Button
          variant="primary"
          size="sm"
          stretch="auto"
          disabled={saving || !dirty}
          onClick={() => void save(draft)}
          label={saving ? "Saving…" : "Save"}
        />
        {confirmDiscard ? (
          <span className="flex items-center gap-2">
            <span className="text-muted-foreground text-caption-md-regular">Discard changes?</span>
            <Button
              variant="danger-outline"
              size="sm"
              stretch="auto"
              onClick={() => setEditing(false)}
              label="Discard"
            />
            <Button
              variant="ghost"
              size="sm"
              stretch="auto"
              onClick={() => setConfirmDiscard(false)}
              label="Keep editing"
            />
          </span>
        ) : (
          <Button
            variant="ghost"
            size="sm"
            stretch="auto"
            onClick={() => (dirty ? setConfirmDiscard(true) : setEditing(false))}
            label="Cancel"
          />
        )}
        {dirty && !confirmDiscard && (
          <span className="text-muted-foreground text-caption-md-regular">unsaved changes</span>
        )}
      </div>
    </div>
  );
}
