/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { Section } from "~/components/kit";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@makeplane/propel/components/table";

type CensusRow = {
  c: string; //   component(s)
  f: string; //   file (basename, relative to the group dir)
  role: string;
  at?: string; // anchor on this page, or a muted note when it can only live in the shell
  ref?: string;
};

type CensusGroup = { title: string; dir: string; blurb: string; rows: CensusRow[] };

const CENSUS: CensusGroup[] = [
  {
    title: "Kit - the house vocabulary",
    dir: "app/components/kit/",
    blurb:
      "Screens import ONLY from the barrel (~/components/kit). If it names a domain pattern, it lives here.",
    rows: [
      {
        c: "PageHeader · Section · EmptyState · LoadingState · Pill · Rows · ToggleChip · IconTile · MetaChip · LabeledDivider · FormError · Money · CopyButton · SearchBox · LoadMore · UserBubble · initials",
        f: "primitives.tsx",
        role: "The 17 core primitives",
        at: "#kit",
        ref: "Kit",
      },
      {
        c: "StatusBadge · StatusDot · StatusMark · statusLabel · statusTone",
        f: "status-badge.tsx",
        role: "Status → tone, one mapping; the Mark is the glyph vocabulary (rows/topbar), the Dot the micro mark (timeline)",
        at: "#kit",
        ref: "Kit",
      },
      {
        c: "VerdictBadge",
        f: "verdict-badge.tsx",
        role: "pass / fail / unverified - honest amber, never dressed up",
        at: "#kit",
        ref: "Kit",
      },
      {
        c: "TONE_TINT · TONE_TEXT · HUE_TINT · HUE_BG",
        f: "tone.ts",
        role: "THE tint recipes - status alphas + the 8-hue data ramp",
        at: "#color",
        ref: "Color",
      },
      {
        c: "Liveness",
        f: "liveness.tsx",
        role: "The one liveness mark - the fade trio (replaces the arc app-wide)",
        at: "#kit",
        ref: "Kit",
      },
      {
        c: "Markdown",
        f: "markdown.tsx",
        role: "Token-styled prose for streamed text + previews; live block-memo + tail healing; fence dispatch to code cards + blocks",
        at: "#kit",
        ref: "Kit",
      },
      {
        c: "AnswerCodeBlock",
        f: "answer-code-block.tsx",
        role: "The fence's code card (shiki, copy) - lazy-only, never barrel-exported",
        at: "#kit",
        ref: "Kit",
      },
      {
        c: "EmailBlock · MessageBlock",
        f: "email-block.tsx · message-block.tsx",
        role: "```email / ```message draft cards - copy (rich+plain), Use-this-draft via MarkdownActions",
        at: "#kit",
        ref: "Kit",
      },
      {
        c: "CsvBlock · MermaidBlock",
        f: "csv-block.tsx · mermaid-block.tsx",
        role: "```csv table preview (capped, honest) · ```mermaid diagram (lazy, strict, source fallback)",
        at: "#kit",
        ref: "Kit",
      },
      {
        c: "SPECIAL_BLOCKS · MarkdownActions",
        f: "special-blocks.tsx",
        role: "The fence registry + host-action context; fences are presentation only (ADR-053)",
        at: "#kit",
        ref: "Kit",
      },
      {
        c: "readFence · scanFences",
        f: "fence.ts",
        role: "Pure fence extraction off the hast node + the ordinal/open-tail scan",
        at: "#kit",
        ref: "Kit",
      },
      {
        c: "StepTimeline",
        f: "step-timeline.tsx",
        role: "The plan as a dotted thread (task rail)",
        at: "#kit",
        ref: "Kit",
      },
      {
        c: "artifactIcon · artifactKindLabel",
        f: "artifact-icon.ts",
        role: "Wire type → glyph + human name",
        at: "#kit",
        ref: "Kit",
      },
      {
        c: "ComingSoon",
        f: "coming-soon.tsx",
        role: "An unbuilt screen reads as intentional",
        at: "#kit",
        ref: "Kit",
      },
      {
        c: "QuietDisclosure · DisclosureChevron",
        f: "disclosure.tsx",
        role: "The house expandable line + the rotating chevron every disclosure trigger wears",
        at: "task rail · tree",
      },
      {
        c: "(barrel)",
        f: "index.ts",
        role: "The one import path screens are allowed",
        at: "everywhere",
      },
    ],
  },
  {
    title: "Propel - Plane's primitives",
    dir: "@makeplane/propel/components/",
    blurb:
      "Imported directly, one subpath per component, as Plane's apps do. Content is a prop, every variant axis is passed, and no Propel component takes a className or style (ADR-074).",
    rows: [
      {
        c: "Button · IconButton",
        f: "button · icon-button",
        role: "Every action - label and icon are props; an IconButton names itself",
        at: "#propel",
        ref: "Propel",
      },
      {
        c: "Badge",
        f: "badge",
        role: "Kind, role and family labels",
        at: "#propel",
        ref: "Propel",
      },
      {
        c: "InputField · TextAreaField · SelectField",
        f: "input-field · text-area-field · select-field",
        role: "Labelled text fields and pickers",
        at: "#propel",
        ref: "Propel",
      },
      {
        c: "Input · TextArea · Select · Field · Fieldset",
        f: "input · text-area · select · field · fieldset",
        role: "The bare parts, for controls the fields cannot frame",
        at: "shell",
      },
      {
        c: "Checkbox · CheckboxGroupField · RadioGroupField · Radio · Switch",
        f: "checkbox · checkbox-group-field · radio-group-field · radio · switch",
        role: "Choice controls - lawful circles",
        at: "#propel",
        ref: "Propel",
      },
      { c: "Tabs", f: "tabs", role: "Segmented panes", at: "#propel", ref: "Propel" },
      {
        c: "Skeleton · LinearProgress · Separator",
        f: "skeleton · linear-progress · separator",
        role: "States + rules",
        at: "#propel",
        ref: "Propel",
      },
      {
        c: "Avatar · AvatarGroup",
        f: "avatar · avatar-group",
        role: "People - always circular; kit AvatarStack wraps the group",
        at: "#propel",
        ref: "Propel",
      },
      { c: "Breadcrumb", f: "breadcrumb", role: "Trail chrome", at: "#propel", ref: "Propel" },
      {
        c: "Table",
        f: "table",
        role: "Flow rows - this census speaks it (the collection's table lens stays a plain table, ADR-074)",
        at: "#census",
        ref: "Census",
      },
      {
        c: "Tooltip · Shortcut",
        f: "tooltip · shortcut",
        role: "Hover hints",
        at: "#propel",
        ref: "Propel",
      },
      {
        c: "Menu",
        f: "menu",
        role: "Action menus - a label lives inside its group",
        at: "#propel",
        ref: "Propel",
      },
      {
        c: "Popover · PreviewCard",
        f: "popover · preview-card",
        role: "Anchored panels · hover previews",
        at: "#propel",
        ref: "Propel",
      },
      {
        c: "Dialog · AlertDialog",
        f: "dialog · alert-dialog",
        role: "Modal overlays; every yes/no is an AlertDialog",
        at: "#propel",
        ref: "Propel",
      },
      {
        c: "Combobox",
        f: "combobox",
        role: "Search and pick (the composer's project picker)",
        at: "#propel",
        ref: "Propel",
      },
      { c: "Drawer", f: "drawer", role: "Edge panels - frames the artifact peek", at: "shell" },
      {
        c: "Icon",
        f: "icon",
        role: "Carries a lucide or hugeicons glyph into every Propel icon slot",
        at: "everywhere",
      },
    ],
  },
  {
    title: "Blocks - Prismoid's composites",
    dir: "packages/blocks/src/ (@prismoid/blocks)",
    blurb:
      "Prismoid's own pieces built from Propel parts, one subpath export each, as Plane's @plane/blocks. The root export is the design system stylesheet.",
    rows: [
      {
        c: "Sidebar · 24 pieces",
        f: "sidebar/",
        role: "The shell's nav frame (needs SidebarProvider)",
        at: "shell",
      },
      {
        c: "MessageScroller · 6 pieces",
        f: "message-scroller/",
        role: "Transcript tail-follow scroller",
        at: "shell",
      },
      {
        c: "PromptInput · 8 pieces",
        f: "prompt-input/",
        role: "The composer primitive (2xl box, square ↑ send)",
        at: "#blocks",
        ref: "Blocks",
      },
      {
        c: "Bubble · Attachment (9 pieces)",
        f: "bubble/ · attachment/",
        role: "Backs kit UserBubble (the user's chat bubble) · composer attachments",
        at: "task page · inbox",
      },
      {
        c: "CodeBlock",
        f: "code-block/",
        role: "Shiki-highlighted source",
        at: "#blocks",
        ref: "Blocks",
      },
      {
        c: "Marker · 3 pieces",
        f: "marker/",
        role: "Quiet phase markers",
        at: "#blocks",
        ref: "Blocks",
      },
      { c: "Spinner", f: "spinner/", role: "The busy glyph", at: "#blocks", ref: "Blocks" },
      {
        c: "Card · 7 pieces",
        f: "card/",
        role: "The surface - Propel has none",
        at: "#blocks",
        ref: "Blocks",
      },
      {
        c: "toast · PrismoidToastProvider",
        f: "toast/",
        role: "Toasts on Propel's, in sonner's call shape; the provider is mounted once in root.tsx",
        at: "#blocks",
        ref: "Blocks",
      },
      {
        c: "cn",
        f: "internal/cn.ts",
        role: "The Propel-aware class merge (web's cn re-exports it)",
        at: "everywhere",
      },
    ],
  },
  {
    title: "Layout - shell chrome",
    dir: "app/components/layout/",
    blurb: "The frame around every screen. Composes ui + kit; invents nothing visual.",
    rows: [
      {
        c: "AppSidebar",
        f: "app-sidebar.tsx",
        role: "Nav + account chip + recents window",
        at: "shell",
      },
      {
        c: "Breadcrumbs",
        f: "breadcrumbs.tsx",
        role: "handle.crumb trail - last crumb is the title",
        at: "shell",
      },
      {
        c: "SettingsDialog + panels",
        f: "settings-dialog.tsx · settings-panels.tsx",
        role: "Notion-style settings (general/usage/skills/models/connectors…)",
        at: "shell",
      },
      {
        c: "SidebarRecentTasks",
        f: "sidebar-recent-tasks.tsx",
        role: "Recents/pins with rename + delete dialogs (the sidebar-section pattern, DESIGN §)",
        at: "shell",
      },
      {
        c: "SidebarProjects",
        f: "sidebar-projects.tsx",
        role: "The Projects window - faces + names, same sidebar-section anatomy",
        at: "shell",
      },
      {
        c: "AuthCard",
        f: "auth-card.tsx",
        role: "The login/signup frame (rounded-xl card)",
        at: "login · signup",
      },
      { c: "RouteError", f: "route-error.tsx", role: "The error boundary screen", at: "on crash" },
      {
        c: "TopbarPortal",
        f: "topbar-slot.tsx",
        role: "Header slot - pages project search · actions · telemetry (task page, /tasks, /projects)",
        at: "shell",
      },
      {
        c: "SectionTab",
        f: "section-tab.tsx",
        role: "Section-tab scaffold - min-h-8 control row + space-y-4 content, one rhythm across tabs",
        at: "shell",
      },
      {
        c: "ModeToggle",
        f: "mode-toggle.tsx",
        role: "Light/dark - top right of this page",
        at: "#top",
        ref: "here",
      },
      {
        c: "DevFooter",
        f: "dev-footer.tsx",
        role: "API liveness capsule - bottom-right in dev builds",
        at: "live now",
      },
    ],
  },
  {
    title: "Features - artifact",
    dir: "app/components/artifact/",
    blurb:
      "Artifact display. registry.ts dispatches on the SERVER's render hint - adding a kind = one renderer file + one registry line.",
    rows: [
      {
        c: "ArtifactView",
        f: "artifact-view.tsx",
        role: "The /artifacts/:id screen body",
        at: "shell",
      },
      {
        c: "ArtifactReader",
        f: "artifact-reader.tsx",
        role: "Resizable reader (task page + project tab + inbox)",
        at: "shell",
      },
      {
        c: "ArtifactCard",
        f: "artifact-card.tsx",
        role: "The deliverable row that opens the reader (transcript + inbox)",
        at: "shell",
      },
      {
        c: "DownloadCard",
        f: "download-card.tsx",
        role: "The floor every binary kind stands on",
        at: "#renderers",
        ref: "Renderers",
      },
      {
        c: "renderer registry + boundary",
        f: "renderers/registry.ts · renderer-boundary.tsx",
        role: "Hint → renderer map; crash floor",
        at: "#renderers",
        ref: "Renderers",
      },
      {
        c: "MarkdownView · TextView",
        f: "renderers/markdown-view.tsx · text-view.tsx",
        role: "document / text kinds",
        at: "shell",
      },
      {
        c: "PreviewView (lazy)",
        f: "renderers/preview-view.tsx",
        role: "The render sidecar, every kind: sheet grid · document blocks · deck slides · code files · the page in its sandboxed iframe (ADR-022)",
        at: "shell",
      },
      {
        c: "RenditionView (lazy)",
        f: "renderers/rendition-view.tsx",
        role: "The full-fidelity rendition (ADR-023): LibreOffice PDF in the browser's viewer · sheet HTML in the sandbox=\"\" cage; render:'pdf' rides it, DownloadCard degraded on failure",
        at: "shell",
      },
      {
        c: "CollectionRenderer (lazy chunk)",
        f: "renderers/collection/collection-view.tsx",
        role: "The collection kind - six lenses, staged edits",
        at: "#renderers",
        ref: "Renderers",
      },
      {
        c: "ViewShell",
        f: "renderers/collection/view-shell.tsx",
        role: "The single-scroller contract every lens obeys",
        at: "#renderers",
        ref: "Renderers",
      },
      {
        c: "core - applyView · edit-state · date-math · use-revise",
        f: "renderers/collection/core/",
        role: "Pure projection + invertible commands + save/rebase",
        at: "logic",
      },
      {
        c: "cells + editors",
        f: "renderers/collection/cells/",
        role: "Type-aware cells; person avatars stay circles",
        at: "#renderers",
        ref: "Renderers",
      },
      {
        c: "toolbar - views · filter · sort · fields · group · chips",
        f: "renderers/collection/toolbar/",
        role: "Rule pills ride rounded-pill",
        at: "#renderers",
        ref: "Renderers",
      },
      {
        c: "RecordPeek",
        f: "renderers/collection/peek/",
        role: "In-layout record panel (Esc/↑↓)",
        at: "#renderers",
        ref: "Renderers",
      },
      {
        c: "six lenses - table · board · list · gallery · gantt · calendar",
        f: "renderers/collection/{table,board,list,gallery,gantt,calendar}/",
        role: "One directory per lens over one projection",
        at: "#renderers",
        ref: "Renderers",
      },
      {
        c: "fixtures",
        f: "renderers/collection/collection.fixtures.ts",
        role: "One fixture, shared by tests and this page",
        at: "#renderers",
        ref: "Renderers",
      },
    ],
  },
  {
    title: "Features - icon picker",
    dir: "app/components/icon-picker/",
    blurb:
      "The project's face. Wire grammar + curated catalogs (data, not logic) + the display atom + the picker.",
    rows: [
      {
        c: "ProjectIcon",
        f: "project-icon.tsx",
        role: "The face atom - hue well + glyph/emoji, folder fallback (project rows + room header)",
        at: "shell",
      },
      {
        c: "IconPicker",
        f: "icon-picker.tsx",
        role: "Icons/Emoji tabs · search · hue row · grid - content-only, mounts in any surface",
        at: "shell",
      },
      {
        c: "value + catalogs",
        f: "value.ts · catalog.ts · emoji.ts",
        role: "lucide:/emoji: grammar; adding an icon = one data row",
        at: "logic",
      },
    ],
  },
  {
    title: "Features - tasks · composer · memory",
    dir: "app/components/{tasks,composer,memory}/",
    blurb:
      "The task browser, the gate/question surfaces (two-consumer promotions), the landing composer and its dials, and the memory ledger row.",
    rows: [
      {
        c: "TaskBrowser",
        f: "tasks/task-browser/ (browser · toolbar · pieces · data)",
        role: "List/board layouts, group-by, filter chips - /tasks + a project's Tasks tab",
        at: "shell",
      },
      {
        c: "GateCard",
        f: "tasks/gate-card.tsx",
        role: "The question batch ALL AT ONCE - accept/decline/cancel; the inbox's sibling of QuestionDock (a pane answers in place; a stepper would fight its scroll)",
        at: "shell",
      },
      {
        c: "GateDock",
        f: "tasks/gate-dock.tsx",
        role: "The one switch on dockFor that every gate host mounts (task page, peek, bot chat card): connect, approve, budget (where the host passes the task's figures), questions, or nothing",
        at: "shell",
      },
      {
        c: "QuestionDock",
        f: "tasks/question-dock.tsx",
        role: 'The task page\'s gate - ONE question at a time, docked above the composer; "Or reply directly…" routes composer text here',
        at: "shell",
      },
      {
        c: "ApprovalDock",
        f: "tasks/approval-dock.tsx",
        role: "The approve gate's face (ADR-054) - Allow/Deny over per-call evidence; QuestionDock's sibling, one shell recipe",
        at: "shell",
      },
      {
        c: "ConnectDock",
        f: "tasks/connect-dock.tsx",
        role: "The connect gate's face (ADR-054) - its OWN species, never Q&A chrome: CONNECTION NEEDED kicker, lg brand well, one Connect action, self-accepting on live",
        at: "shell",
      },
      {
        c: "TaskWindowProvider",
        f: "tasks/task-window.tsx",
        role: "One recents/pins window for sidebar + Home",
        at: "shell",
      },
      {
        c: "Composer (+ capture)",
        f: "composer/composer.tsx · capture.ts",
        role: "The landing's one box; its primitive shows in § Blocks",
        at: "shell",
      },
      {
        c: "TaskComposer",
        f: "composer/task-composer.tsx",
        role: "The task-minting box with every dial: the landing's (its own Project picker) and a Project page's (scoped to that Project) - one component, one submit path",
        at: "/ · /projects/:id",
      },
      {
        c: "OptionsMenu · ClarifyDial · BudgetDial · SkillsDial",
        f: "composer/options-menu.tsx · task-dials.tsx",
        role: "The box's one Options menu (every setting a row with its value and a submenu) and the three pills only this box carries, shown in the row once set",
        at: "/ · /projects/:id",
      },
      {
        c: "ProjectPicker · WhenDial",
        f: "composer/project-picker.tsx · when-control.tsx",
        role: "The composer's destination and When dials (promoted from routes/home on the second composer)",
        at: "shell",
      },
      {
        c: "ModeDial · FleetModelsDial",
        f: "composer/dials.tsx",
        role: "The Standard/Fleet mode pill + the fleet seats picker - the composer's dials",
        at: "shell",
      },
      {
        c: "ConnectorsControl",
        f: "composer/connectors-control.tsx",
        role: "The connectors dial (ADR-020) - per-connector switches + Add/Manage; a row in Options always, a pill in the row once one is attached",
        at: "shell",
      },
      {
        c: "MemoryRowItem",
        f: "memory/memory-row.tsx",
        role: "One memory ledger row - kind Badge, edit-in-place + retire (Settings → Memory, a project's Context)",
        at: "shell",
      },
    ],
  },
  {
    title: "Connectors - the service vocabulary",
    dir: "app/components/connectors/",
    blurb:
      "ADR-054: the catalog wire adapter, the shared OAuth dance, per-tool policy logic, the suggestion grammar, and the service faces - one home, consumed by /connectors, the settings panel, and both composers.",
    rows: [
      {
        c: "vocabulary",
        f: "connectors/vocabulary.ts",
        role: "CONNECTOR_TONE (exhaustive over the wire union) · status labels · connectorVerbs · settleFlow",
        at: "shell",
      },
      {
        c: "useConnectFlows",
        f: "connectors/use-connect-flows.ts",
        role: "The OAuth dance, shared: claim tab in the gesture, aim, poll, TTL settle; one-click connectFromCatalog",
        at: "shell",
      },
      {
        c: "ConnectorIcon + BRAND",
        f: "connectors/connector-icon.tsx · brand-icons.ts",
        role: "Hue well + vendored monochrome brand mark (Simple Icons, currentColor); Plug fallback",
        at: "shell",
      },
      {
        c: "catalog adapter",
        f: "connectors/catalog.ts",
        role: "fetchCatalog · mergeCatalog (roster wins) · filterCards · the policy/auto-attach mutations",
        at: "shell",
      },
      {
        c: "policies",
        f: "connectors/policies.ts",
        role: "POLICY_META (total over allow|ask|off) · read-only vs write grouping · replace-semantics builders",
        at: "shell",
      },
      {
        c: "suggestConnector + chip",
        f: "connectors/suggest.ts · suggestion-chip.tsx",
        role: "The word-boundary intent matcher (the server door's own grammar) + the composer's quiet offer",
        at: "shell",
      },
    ],
  },
  {
    title: "Routes - the app map",
    dir: "app/routes/ · app/routes.ts",
    blurb:
      "routes.ts reads top-to-bottom as the entire application. One directory per screen; composition only.",
    rows: [
      {
        c: "login · signup · logout",
        f: "login/ · signup/ · logout/",
        role: "Public auth, outside the shell",
        at: "public",
      },
      {
        c: "shell (guard + container)",
        f: "shell/route.tsx · shell/container.tsx",
        role: "Auth guard layout; standard page frame",
        at: "wrapper",
      },
      { c: "home", f: "home/", role: "The landing - describe, guard, send", at: "/" },
      { c: "tasks", f: "tasks/", role: "The browser over TaskBrowser", at: "/tasks" },
      {
        c: "tasks.$id",
        f: "tasks.$id/",
        role: "The live task view - the flagship screen; a Project task's canonical URL is the nested one",
        at: "/tasks/:id · /projects/:project/tasks/:id",
      },
      {
        c: "projects · projects.$id",
        f: "projects/ · projects.$id/",
        role: "Rooms - list + tabs (Tasks/Artifacts/Settings)",
        at: "/projects",
      },
      {
        c: "connectors",
        f: "connectors/",
        role: "The service catalog + per-tool permissions (ADR-054) - cards, peek, one-click OAuth",
        at: "/connectors",
      },
      {
        c: "artifacts.$id",
        f: "artifacts.$id/",
        role: "Artifact + verification report",
        at: "/artifacts/:id",
      },
      {
        c: "inbox",
        f: "inbox/",
        role: "Linear-style two-pane; questions + deliverables actionable in place",
        at: "/inbox",
      },
      {
        c: "ds (this page)",
        f: "ds/route.tsx",
        role: "The design system - linked from nowhere",
        at: "/ds",
      },
    ],
  },
];

/** The census "where" cell: an anchor chip when it renders on this page, muted words when not. */
function WhereCell({ at, refLabel }: { at?: string; refLabel?: string }) {
  if (at?.startsWith("#")) {
    return (
      <a
        href={at}
        className="ring-foreground/10 text-muted-foreground hover:text-foreground inline-flex rounded-pill px-2 py-0.5 text-caption-md-regular whitespace-nowrap ring-1 ring-inset transition-colors"
      >
        § {refLabel}
      </a>
    );
  }
  return <span className="text-muted-foreground/70 text-caption-md-regular">{at ?? "-"}</span>;
}

export function CensusSection() {
  return (
    <section id="census">
      <Section title="Census - every component, its file, its role">
        <p className="text-muted-foreground max-w-2xl text-body-sm-regular">
          The complete inventory, one table per home. “shell” means it composes live app state
          (loaders, sockets, the sidebar) and renders only inside the real app; § chips jump to
          where it shows above.
        </p>
        <div className="space-y-8">
          {CENSUS.map((g) => (
            <div key={g.title} className="space-y-2">
              <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                <h3 className="text-body-sm-regular font-medium">{g.title}</h3>
                <code className="text-muted-foreground/70 font-mono text-caption-md-regular">
                  {g.dir}
                </code>
              </div>
              <p className="text-muted-foreground max-w-2xl text-caption-md-regular">{g.blurb}</p>
              <Table variant="table">
                <TableHeader>
                  <TableRow>
                    <TableHead label="Component" />
                    <TableHead label="File" />
                    <TableHead label="Role" />
                    <TableHead label="Where" />
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {g.rows.map((r) => (
                    <TableRow key={r.f + r.c}>
                      <TableCell>
                        <span className="font-medium whitespace-normal">{r.c}</span>
                      </TableCell>
                      <TableCell>
                        <code className="text-muted-foreground font-mono text-caption-md-regular whitespace-normal">
                          {r.f}
                        </code>
                      </TableCell>
                      <TableCell>
                        <span className="text-muted-foreground whitespace-normal">{r.role}</span>
                      </TableCell>
                      <TableCell>
                        <WhereCell at={r.at} refLabel={r.ref} />
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          ))}
        </div>
      </Section>
    </section>
  );
}
