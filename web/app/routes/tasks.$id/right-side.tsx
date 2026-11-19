/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { X } from "lucide-react";
import type { ComponentProps } from "react";
import {
  Dialog,
  DialogBody,
  DialogClose,
  DialogCloseGroup,
  DialogContent,
  DialogTitle,
} from "@makeplane/propel/components/dialog";
import {
  Drawer,
  DrawerBody,
  DrawerClose,
  DrawerHeader,
  DrawerHeaderActions,
  DrawerHeaderContent,
  DrawerPanel,
  DrawerTitle,
} from "@makeplane/propel/components/drawer";
import { Icon } from "@makeplane/propel/components/icon";
import { IconButton } from "@makeplane/propel/components/icon-button";
import { ArtifactReader } from "~/components/artifact/artifact-reader";
import type { components } from "~/lib/api/types.gen";
import { knownProjectName } from "~/lib/project-names";
import type { GateView, ScreenLease, TaskView } from "~/lib/task-view";
import { ReaderPane } from "./reader-pane";
import { ScreenPane } from "./screen-pane";
import { TaskRail } from "./task-rail";
import type { RightSurface } from "./use-right-surface";

type TaskDetailOut = components["schemas"]["TaskDetailOut"];
export type RailProps = ComponentProps<typeof TaskRail>;

export type RightSideProps = {
  surface: RightSurface;
  detail: TaskDetailOut;
  view: Pick<TaskView, "screenAction" | "controlEnded" | "status">;
  lease: ScreenLease | null;
  /** The open CONTROL gate the screen's banner reads (live-gated, like every dock). */
  controlGate: GateView | null;
  myId?: string;
  /** ONE rail content, three homes - floating card · narrow sheet · reader popover (the
   *  contract at the top of task-rail.tsx). Assembled once so no mount can drift. */
  railProps: RailProps;
};

/** The screen pane, wide or in the narrow drawer (watch only there: driving needs a keyboard). */
function TaskScreen({
  p,
  machine,
  onClose,
  narrow,
}: {
  p: RightSideProps;
  machine: NonNullable<TaskDetailOut["machine"]>;
  onClose: () => void;
  narrow?: boolean;
}) {
  return (
    <ScreenPane
      taskId={p.detail.id}
      machine={machine}
      canEdit={p.detail.can_edit}
      isOwner={p.detail.can_end_control}
      me={p.myId ?? ""}
      gate={p.controlGate}
      onClose={onClose}
      lease={p.lease}
      action={p.view.screenAction}
      ended={p.view.controlEnded}
      // The claim menu's warning names the Project; a personal task has none.
      projectName={
        p.detail.project_id ? (knownProjectName(p.detail.project_id) ?? "this Project") : null
      }
      narrow={narrow}
      taskStatus={p.view.status}
    />
  );
}

/**
 * The wide right edge, one surface at a time (the law lives in use-right-surface.ts):
 *   reader open → the SPLIT PANE pushes the transcript; the rail escalates to the centered
 *                 modal behind the band's Progress button
 *   screen open → the screen takes the same split slot
 *   otherwise   → the FLOATING RAIL card, per the saved preference
 */
function WideSurface(p: RightSideProps) {
  const { surface, detail } = p;
  const canEdit = detail.can_edit;
  if (surface.openArtifact)
    return (
      <ReaderPane
        artifactId={surface.openArtifact}
        kindHint={surface.openKind}
        title={surface.openEntry?.title}
        verdict={surface.openEntry?.verdict}
        pct={surface.panePct}
        onPctCommit={surface.commitSplitPct}
        onClose={surface.closeReader}
        // A save/restore mints a new head and the reducer re-keys the shelf entry in place -
        // repoint the open id or the band's title/verdict go blank. A viewer writes nothing:
        // no edit, restore or label is offered.
        onArtifactChanged={canEdit ? surface.setOpenArtifact : undefined}
        readOnly={!canEdit}
      />
    );
  if (surface.screenOpen && detail.machine)
    return <TaskScreen p={p} machine={detail.machine} onClose={surface.closeScreen} />;
  if (!surface.railPref) return null;
  return (
    <aside className="bg-popover ring-foreground/10 scrollbar-thin my-3 mr-3 min-h-0 w-80 shrink-0 overflow-y-auto overscroll-contain rounded-xl shadow-(--shadow-overlay) ring-1">
      <TaskRail {...p.railProps} />
    </aside>
  );
}

/** The rail's MODAL home (artifact mode): the right edge belongs to the artifact, so Progress
 *  · Outputs · Sources · Usage escalate to a dialog - same content, one click from the
 *  topbar's Progress button. Picking an output swaps the reader. */
function RailModal({ surface, railProps }: RightSideProps) {
  return (
    <Dialog open={surface.railModal && surface.artifactMode} onOpenChange={surface.setRailModal}>
      <DialogContent size="xs">
        <div className="sr-only">
          <DialogTitle>Progress and usage</DialogTitle>
        </div>
        {/* No DialogMain: the rail brings its own gutters, the body only scrolls. */}
        <DialogBody>
          <TaskRail
            {...railProps}
            onOpenArtifact={(id) => {
              surface.setRailModal(false);
              surface.openArtifactFromRail(id);
            }}
          />
        </DialogBody>
        <DialogCloseGroup>
          <IconButton
            variant="ghost"
            size="xs"
            aria-label="Close"
            icon={<Icon icon={X} />}
            render={<DialogClose />}
          />
        </DialogCloseGroup>
      </DialogContent>
    </Dialog>
  );
}

/** Narrow screens: a split cannot fit, so the rail, the screen and the reader are overlays. */
function NarrowSurfaces(p: RightSideProps) {
  const { surface, detail } = p;
  const canEdit = detail.can_edit;
  return (
    <>
      <Drawer
        open={surface.railSheet}
        onOpenChange={surface.setRailSheet}
        modal
        swipeDirection="right"
      >
        <DrawerPanel variant="overlay" side="end" backdrop>
          <DrawerHeader>
            <DrawerHeaderContent>
              <DrawerTitle>Progress &amp; outputs</DrawerTitle>
            </DrawerHeaderContent>
            <DrawerHeaderActions side="end">
              <DrawerClose
                render={
                  <IconButton
                    variant="ghost"
                    size="md"
                    aria-label="Close"
                    icon={<Icon icon={X} />}
                  />
                }
              />
            </DrawerHeaderActions>
          </DrawerHeader>
          <DrawerBody>
            {/* The drawer owns the gutters here. */}
            <TaskRail {...p.railProps} className="p-0" />
          </DrawerBody>
        </DrawerPanel>
      </Drawer>
      {/* The screen on a narrow screen: a wide drawer, watch only (driving needs a keyboard). */}
      {detail.machine && (
        <Drawer
          open={surface.screenOpen}
          onOpenChange={(open) => !open && surface.setOpenScreen(false)}
          modal
          swipeDirection="right"
        >
          <DrawerPanel variant="overlay" side="end" size="lg" backdrop>
            <div className="sr-only">
              <DrawerTitle>The screen</DrawerTitle>
            </div>
            <TaskScreen
              p={p}
              machine={detail.machine}
              onClose={() => surface.setOpenScreen(false)}
              narrow
            />
          </DrawerPanel>
        </Drawer>
      )}
      <ArtifactReader
        artifactId={surface.openArtifact}
        onClose={surface.closeReader}
        kindHint={surface.openKind}
        onArtifactChanged={canEdit ? surface.setOpenArtifact : undefined}
        readOnly={!canEdit}
      />
    </>
  );
}

/** THE RIGHT SIDE - one surface at a time; narrow screens keep the overlay set instead. */
export function RightSide(p: RightSideProps) {
  if (!p.surface.wide) return <NarrowSurfaces {...p} />;
  return (
    <>
      <WideSurface {...p} />
      <RailModal {...p} />
    </>
  );
}
