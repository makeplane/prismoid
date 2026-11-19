/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { FileText, Search, Settings2 } from "lucide-react";
import { useState } from "react";
import {
  ComingSoon,
  CopyButton,
  EmptyState,
  FormError,
  IconTile,
  LabeledDivider,
  LoadMore,
  Liveness,
  LoadingState,
  Markdown,
  MarkdownActionsProvider,
  MetaChip,
  Money,
  PageHeader,
  Pill,
  Rows,
  SearchBox,
  Section,
  StatusBadge,
  StatusDot,
  StatusMark,
  StepTimeline,
  ToggleChip,
  UserBubble,
  VerdictBadge,
  artifactIcon,
  artifactKindLabel,
  initials,
} from "~/components/kit";
import { Button } from "@makeplane/propel/components/button";
import { Specimen, TONES } from "./shared";

const TASK_STATUSES = [
  "created",
  "planning",
  "clarifying",
  "running",
  "waiting",
  "delivered",
  "failed",
  "cancelled",
];
const STEP_STATES = ["pending", "running", "verifying", "done", "retrying", "escalated", "skipped"];
const KINDS = [
  "document",
  "spreadsheet",
  "deck",
  "code",
  "webapp",
  "dataset",
  "collection",
  "fleet_report",
];
const TIMELINE_FIXTURE = [
  { id: "s1", title: "Read the brief and sources", state: "done", agent: "researcher", attempt: 1 },
  { id: "s2", title: "Draft the memo", state: "running", agent: "writer", attempt: 2 },
  { id: "s3", title: "Verify and sign", state: "pending", agent: null, attempt: 1 },
];

const MARKDOWN_SAMPLE = `A **markdown** sample with \`inline code\`, ~~struck text~~, a [link](#kit), and an
[external link](https://example.com) that opens in its own tab:

- a list item
- another with a nested list
  - one level down

1. an ordered item
   with a continuation line
2. and its sibling

- [x] a settled task item
- [ ] an open one

#### An h4 that must not look like a paragraph
##### An h5 in role
###### An h6, quiet

> A blockquote whose edges sit flush against its rule.

\`\`\`ts
export function greet(name: string): string {
  return \`hello, \${name}\`; // highlighted by shiki, copied by the header button
}
\`\`\`

| kind | radius |
| --- | --- |
| chip | pill |
| card | lg |

A footnote lands in the quiet appendix below.[^1]

[^1]: The appendix itself.`;

const BLOCKS_SAMPLE = `Two ways to say it:

\`\`\`email
To: alex@client.example
Subject: The launch moves to Tuesday
Hi Alex,

The launch moves to **Tuesday** - the extra day buys us the final QA pass.

Can you confirm the window still works on your side?
\`\`\`

\`\`\`email
To: alex@client.example
Subject: Tuesday it is
Alex - quick one: we're shifting the launch to Tuesday for a last QA pass. Shout if that
collides with anything.
\`\`\`

A ping for the room:

\`\`\`message
Channel: #launch
Heads up: launch moves to **Tuesday**. QA pass lands Monday EOD.
\`\`\`

The dates, as data:

\`\`\`csv
milestone,owner,date
QA pass,Priya,Mon
Launch,Alex,Tue
Retro,Everyone,Fri
\`\`\`

And the flow:

\`\`\`mermaid
graph LR
  QA[QA pass] --> Launch --> Retro
\`\`\``;

const STREAMING_SAMPLE = `Drafting now:

\`\`\`email
To: alex@client.example
Subject: The launch
Hi Alex,

The launch moves to Tu`;

export function KitSection() {
  const [query, setQuery] = useState("");
  return (
    <section id="kit">
      <Section title="Kit - the house vocabulary (app/components/kit)">
        <div className="space-y-4">
          <Specimen label="PageHeader" file="kit/primitives.tsx">
            <div className="ring-foreground/10 w-full max-w-md rounded-lg p-4 ring-1">
              <PageHeader
                as="h2"
                title="A drawer heading"
                description="With a description and actions."
                actions={<Button variant="primary" size="sm" stretch="auto" label="Action" />}
                className="pb-0"
              />
            </div>
          </Specimen>
          <Specimen label="Pill (12px)" file="kit/primitives.tsx">
            {TONES.map((t) => (
              <Pill key={t} tone={t} icon={t === "success" ? FileText : undefined}>
                {t}
              </Pill>
            ))}
          </Specimen>
          <Specimen label="StatusBadge · task" file="kit/status-badge.tsx">
            {TASK_STATUSES.map((s) => (
              <StatusBadge key={s} status={s} />
            ))}
          </Specimen>
          <Specimen label="StatusBadge · step" file="kit/status-badge.tsx">
            {STEP_STATES.map((s) => (
              <StatusBadge key={s} status={s} />
            ))}
          </Specimen>
          <Specimen
            label="StatusMark"
            file="kit/status-badge.tsx"
            note="State as a GLYPH (task rows, board columns, topbar): working spins, needs-input speaks, queued waits, settled shows its verdict."
          >
            {TASK_STATUSES.map((s) => (
              <span key={s} className="inline-flex items-center gap-1.5 text-caption-md-regular">
                <StatusMark status={s} decorative /> {s}
              </span>
            ))}
          </Specimen>
          <Specimen
            label="StatusDot"
            file="kit/status-badge.tsx"
            note="The micro mark (step timeline): hollow = not yet run · ripple = live · solid = settled."
          >
            {TASK_STATUSES.map((s) => (
              <span key={s} className="inline-flex items-center gap-1.5 text-caption-md-regular">
                <StatusDot status={s} decorative /> {s}
              </span>
            ))}
          </Specimen>
          <Specimen label="VerdictBadge" file="kit/verdict-badge.tsx">
            <VerdictBadge verdict="pass" />
            <VerdictBadge verdict="fail" />
            <VerdictBadge verdict="unverified" />
            <VerdictBadge verdict={null} />
          </Specimen>
          <Specimen label="ToggleChip" file="kit/primitives.tsx">
            <ToggleChip active>active</ToggleChip>
            <ToggleChip>idle</ToggleChip>
          </Specimen>
          <Specimen label="MetaChip" file="kit/primitives.tsx">
            <MetaChip icon={FileText}>memo.md</MetaChip>
            <MetaChip icon={Search}>qwen · cross-family</MetaChip>
            <MetaChip>12 rows</MetaChip>
          </Specimen>
          <Specimen label="IconTile" file="kit/primitives.tsx">
            <IconTile icon={FileText} />
            <IconTile icon={Settings2} tone="primary" />
          </Specimen>
          <Specimen label="Money" file="kit/primitives.tsx">
            <Money usd="0.0382" /> · <Money usd="0.000191" /> · <Money usd="0" /> ·{" "}
            <Money usd={null} muted />
          </Specimen>
          <Specimen label="CopyButton" file="kit/primitives.tsx">
            <code className="bg-muted rounded-sm px-1.5 py-0.5 font-mono text-caption-md-regular">
              ed25519:9f3a…
            </code>
            <CopyButton value="ed25519:9f3a" />
          </Specimen>
          <Specimen label="artifactIcon" file="kit/artifact-icon.ts">
            {/* The naming law is FLAT: one wire kind, one word - no filename may change it. */}
            {KINDS.map((k) => {
              const Icon = artifactIcon(k);
              return (
                <span key={k} className="inline-flex items-center gap-1.5 text-caption-md-regular">
                  <Icon className="text-muted-foreground size-4" />
                  {artifactKindLabel(k)}
                </span>
              );
            })}
          </Specimen>
          <Specimen label="initials" file="kit/primitives.tsx">
            {["Ada Lovelace", "Prismoid", "x"].map((n) => (
              <span
                key={n}
                className="bg-primary text-primary-foreground flex size-8 items-center justify-center rounded-full text-caption-md-regular font-medium"
              >
                {initials(n)}
              </span>
            ))}
          </Specimen>
          <Specimen label="Rows" file="kit/primitives.tsx">
            <Rows className="w-full max-w-md">
              {["First row", "Second row", "Third row"].map((r) => (
                <div key={r} className="px-4 py-3 text-body-sm-regular">
                  {r}
                </div>
              ))}
            </Rows>
          </Specimen>
          <Specimen
            label="UserBubble"
            file="kit/primitives.tsx"
            note="The user's words in any conversation - task turns, inbox replay. As-typed text, never Markdown."
          >
            <div className="w-full max-w-md space-y-2">
              <p className="text-body-sm-regular">Delivered - six workstreams, grouped by stage.</p>
              <UserBubble>looks good - add a Blocked column</UserBubble>
            </div>
          </Specimen>
          <Specimen label="SearchBox" file="kit/primitives.tsx">
            <SearchBox value={query} onChange={setQuery} placeholder="Search anything…" />
          </Specimen>
          <Specimen label="LoadMore" file="kit/primitives.tsx">
            <LoadMore hasMore busy={false} onClick={() => {}} />
            <LoadMore hasMore busy onClick={() => {}} />
          </Specimen>
          <Specimen label="LabeledDivider" file="kit/primitives.tsx">
            <div className="w-full max-w-md">
              <LabeledDivider>Follow-up</LabeledDivider>
            </div>
          </Specimen>
          <Specimen label="FormError" file="kit/primitives.tsx">
            <FormError>That email is already registered.</FormError>
          </Specimen>
          <Specimen label="LoadingState" file="kit/primitives.tsx">
            <LoadingState className="py-2" />
          </Specimen>
          <Specimen label="EmptyState" file="kit/primitives.tsx">
            <div className="w-full max-w-md space-y-3">
              <EmptyState
                compact
                title="No artifacts yet"
                description="Published work appears here once a task delivers."
              />
            </div>
          </Specimen>
          <Specimen label="StepTimeline" file="kit/step-timeline.tsx">
            <div className="w-full max-w-sm">
              <StepTimeline steps={TIMELINE_FIXTURE} />
            </div>
          </Specimen>
          <Specimen
            label="StepTimeline · settled"
            file="kit/step-timeline.tsx"
            note="A plan that was replaced: nothing ripples, the words go quiet, and a step that was still in motion reads replaced."
          >
            <div className="w-full max-w-sm">
              <StepTimeline steps={TIMELINE_FIXTURE} settled />
            </div>
          </Specimen>
          <Specimen
            label="Liveness"
            file="kit/liveness.tsx"
            note="The one liveness mark (2026-08-29, round two): three dots rippling by opacity alone - no bounce, calm beside tabular figures. Replaces the rotating arc at every product site; reduced motion falls still at readable opacity."
          >
            <div className="text-muted-foreground flex items-center gap-6">
              <span className="flex items-center gap-2">
                <Liveness className="size-3" />
                12
              </span>
              <span className="flex items-center gap-2">
                <Liveness className="size-4" />
                16
              </span>
              <span className="flex items-center gap-2">
                <Liveness className="size-5.5" />
                22
              </span>
              <span className="flex items-center gap-2 text-body-sm-regular">
                <Liveness className="size-3.5" />
                Working - step 2 of 5
              </span>
            </div>
          </Specimen>
          <Specimen label="Markdown" file="kit/markdown.tsx">
            <div className="ring-foreground/10 w-full max-w-md rounded-lg p-4 ring-1">
              <Markdown>{MARKDOWN_SAMPLE}</Markdown>
            </div>
          </Specimen>
          <Specimen
            label="Answer blocks"
            file="kit/special-blocks.tsx"
            note="Fence-language dispatch (ADR-053): email/message drafts, csv preview, mermaid. With a host handler the drafts grow “Use this draft”; without one the button is absent (capability law)."
          >
            <div className="ring-foreground/10 w-full max-w-xl rounded-lg p-4 ring-1">
              <MarkdownActionsProvider
                value={{ onUseDraft: (d) => alert(`composer would receive:\n\n${d}`) }}
              >
                <Markdown>{BLOCKS_SAMPLE}</Markdown>
              </MarkdownActionsProvider>
            </div>
          </Specimen>
          <Specimen
            label="Answer blocks · streaming"
            file="kit/special-blocks.tsx"
            note="The live tail's open fence: progressive render, actions hidden until the fence closes."
          >
            <div className="ring-foreground/10 w-full max-w-xl rounded-lg p-4 ring-1">
              <Markdown live>{STREAMING_SAMPLE}</Markdown>
            </div>
          </Specimen>
          <Specimen label="ComingSoon" file="kit/coming-soon.tsx">
            <div className="ring-foreground/10 w-full max-w-md rounded-lg p-4 ring-1">
              <ComingSoon as="h2" title="A future screen" phase="Phase 2" />
            </div>
          </Specimen>
        </div>
      </Section>
    </section>
  );
}
