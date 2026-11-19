/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import {
  Dialog,
  DialogAside,
  DialogBody,
  DialogClose,
  DialogCloseGroup,
  DialogContent,
  DialogMain,
  DialogPanes,
  DialogTitle,
} from "@makeplane/propel/components/dialog";
import { Icon } from "@makeplane/propel/components/icon";
import { IconButton } from "@makeplane/propel/components/icon-button";
import {
  Brain,
  Building2,
  Cpu,
  Gauge,
  KeyRound,
  LifeBuoy,
  Plug,
  Sparkles,
  UserRound,
  Users,
  X,
} from "lucide-react";
import { useEffect, useState } from "react";
import { ComingSoon, PageHeader } from "~/components/kit";
import { useAuthConfig } from "~/hooks/use-auth-config";
import { WorkspaceGeneralPanel } from "~/components/workspace/workspace-general";
import { WorkspaceMembersPanel } from "~/components/workspace/workspace-members";
import { cn } from "~/lib/utils";
import {
  ApiKeysPanel,
  ConnectorsPanel,
  GeneralPanel,
  MemoryPanel,
  SkillsPanel,
  UsagePanel,
} from "./settings-panels";

export type SettingsSection =
  | "general"
  | "memory"
  | "usage"
  | "workspace-general"
  | "workspace-members"
  | "api-keys"
  | "connectors"
  | "skills"
  | "help";

const GROUPS: {
  label: string;
  items: { id: SettingsSection; label: string; icon: typeof Cpu }[];
}[] = [
  {
    label: "Account",
    items: [
      { id: "general", label: "General", icon: UserRound },
      { id: "memory", label: "Memory", icon: Brain },
      { id: "usage", label: "Usage", icon: Gauge },
    ],
  },
  {
    label: "Workspace",
    items: [
      { id: "workspace-general", label: "General", icon: Building2 },
      { id: "workspace-members", label: "Members", icon: Users },
      { id: "skills", label: "Skills", icon: Sparkles },
      { id: "connectors", label: "Connectors", icon: Plug },
      { id: "api-keys", label: "API keys", icon: KeyRound },
    ],
  },
  {
    label: "Support",
    items: [{ id: "help", label: "Get help", icon: LifeBuoy }],
  },
];

/** Get help: the support address when this deployment sets one, the placeholder when not. */
function HelpPanel() {
  const email = useAuthConfig()?.support_email;
  if (!email) {
    return (
      <ComingSoon as="h2" title="Get help" description="Documentation, support, and the runbook." />
    );
  }
  return (
    <PageHeader
      as="h2"
      title="Get help"
      description={
        <>
          Questions or problems? Email{" "}
          <a
            href={`mailto:${email}`}
            className="text-foreground font-medium underline underline-offset-4"
          >
            {email}
          </a>
          .
        </>
      }
    />
  );
}

/**
 * Settings as one place: a Notion-style dialog - a rail of grouped sections on the left, the
 * active pane on the right. Skills and Models live here now instead of the nav; the sidebar
 * stays about work (home, tasks, projects, inbox), not administration.
 */
export function SettingsDialog({
  open,
  onOpenChange,
  initialSection = "general",
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** The section to land on when the dialog opens - the account menu deep-links here. */
  initialSection?: SettingsSection;
}) {
  const [section, setSection] = useState<SettingsSection>(initialSection);
  useEffect(() => {
    if (open) setSection(initialSection);
  }, [open, initialSection]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {/* xl (1000px) + fixed (700px) is the nearest Propel box to the old 58rem x 42rem. */}
      <DialogContent size="xl" height="fixed">
        <div className="sr-only">
          <DialogTitle>Settings</DialogTitle>
        </div>
        <DialogPanes>
          <DialogAside>
            <nav className="space-y-4 pe-2">
              {GROUPS.map((group) => (
                <div key={group.label}>
                  <p className="text-muted-foreground/70 px-2 pb-1 text-caption-md-regular font-medium">
                    {group.label}
                  </p>
                  <div className="space-y-0.5">
                    {group.items.map((item) => (
                      <button
                        key={item.id}
                        onClick={() => setSection(item.id)}
                        aria-current={section === item.id}
                        className={cn(
                          "text-muted-foreground hover:bg-accent/40 hover:text-foreground flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-body-sm-regular transition-colors",
                          section === item.id && "bg-accent/60 text-foreground",
                        )}
                      >
                        <item.icon className="size-4 shrink-0" />
                        {item.label}
                      </button>
                    ))}
                  </div>
                </div>
              ))}
            </nav>
          </DialogAside>
          <DialogMain>
            <DialogBody tabIndex={0}>
              {section === "general" && <GeneralPanel />}
              {section === "workspace-general" && <WorkspaceGeneralPanel />}
              {section === "workspace-members" && <WorkspaceMembersPanel />}
              {section === "api-keys" && <ApiKeysPanel />}
              {section === "connectors" && <ConnectorsPanel />}
              {section === "memory" && <MemoryPanel />}
              {section === "skills" && <SkillsPanel />}
              {section === "usage" && <UsagePanel />}
              {section === "help" && <HelpPanel />}
            </DialogBody>
          </DialogMain>
        </DialogPanes>
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
