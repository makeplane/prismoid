/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// The app kit - the only vocabulary screens use for domain visuals. Import from "~/components/kit".
export {
  StatusBadge,
  StatusDot,
  StatusMark,
  statusLabel,
  statusSpins,
  statusTone,
  taskMark,
} from "./status-badge";
export { AvatarStack, type StackPerson } from "./avatar-stack";
export { Liveness } from "./liveness";
export { BrandMark, PageLoading } from "./brand";
export { VerdictBadge } from "./verdict-badge";
export {
  Money,
  PageHeader,
  Section,
  EmptyState,
  MetaChip,
  CopyButton,
  Pill,
  Rows,
  ToggleChip,
  IconTile,
  initials,
  LoadMore,
  LoadingState,
  SearchBox,
  LabeledDivider,
  FormError,
  UserBubble,
} from "./primitives";
export {
  TONE_TINT,
  TONE_TEXT,
  TONE_CARD,
  HUE_TINT,
  HUE_BG,
  HUE_TEXT,
  HUE_COUNT,
  nameHue,
  type Tone,
} from "./tone";
export { ComingSoon } from "./coming-soon";
export { DisclosureChevron, QuietDisclosure } from "./disclosure";
export { ImageChip, Markdown } from "./markdown";
// The special-block seam: hosts see the provider and its shape; the registry
// internals stay kit-local imports. AnswerCodeBlock is deliberately NOT exported: it must
// only ever mount through markdown.tsx's React.lazy - a barrel export would let a screen
// pull shiki into an eager chunk.
export { MarkdownActionsProvider, type MarkdownActions } from "./special-blocks";
export { StepTimeline, type TimelineStep } from "./step-timeline";
export { artifactIcon, artifactKindLabel } from "./artifact-icon";
export { SelectOption } from "./select-option";
