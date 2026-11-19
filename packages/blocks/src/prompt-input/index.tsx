/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { Tooltip as BaseTooltip } from "@base-ui/react/tooltip";
import { Shortcut } from "@makeplane/propel/components/shortcut";
import { InputGroup } from "@makeplane/propel/elements/input";
import { TextArea } from "@makeplane/propel/elements/text-area";
import { TooltipPopup } from "@makeplane/propel/elements/tooltip";
import { CornerDownLeftIcon, SquareIcon, XIcon } from "lucide-react";
import {
  Children,
  type ComponentProps,
  type FormEvent,
  type FormEventHandler,
  type HTMLAttributes,
  type KeyboardEventHandler,
  type ReactNode,
} from "react";

import { cn } from "../internal/cn";
import { KitButton, type KitButtonProps, type KitVariant } from "../internal/kit-button";
import { Spinner } from "../spinner";

// A focused, Base-UI-native adaptation of the Vercel AI Elements `prompt-input`. The upstream item
// targets Radix (`asChild`, Radix event/delay props) and bundles attachment/screenshot/model-select
// machinery this app doesn't use - the composer (routes/tasks.new) collects attachments via the core
// `Attachment` component and carries mode/skills/budget as dedicated controls. Kept here: the form
// ergonomics that matter - Enter-to-submit (Shift+Enter for newline, IME-safe), an auto-resizing
// textarea, and a status-aware submit/stop button. Built on Propel's InputGroup, TextArea,
// Button/IconButton and tooltip parts.
//
// `ChatStatus` mirrors the AI SDK union (type-only); this app streams over its own SSE contract.
export type ChatStatus = "submitted" | "streaming" | "ready" | "error";

export type PromptInputMessage = { text: string };

export type PromptInputProps = Omit<HTMLAttributes<HTMLFormElement>, "onSubmit"> & {
  onSubmit?: (
    message: PromptInputMessage,
    event: FormEvent<HTMLFormElement>,
  ) => void | Promise<void>;
};

export const PromptInput = ({ className, onSubmit, children, ...props }: PromptInputProps) => {
  const handleSubmit: FormEventHandler<HTMLFormElement> = (event) => {
    event.preventDefault();
    const text = (new FormData(event.currentTarget).get("message")?.toString() ?? "").trim();
    void onSubmit?.({ text }, event);
  };
  return (
    <form className={cn("w-full", className)} onSubmit={handleSubmit} {...props}>
      {/* Propel's frame, unpadded and gapless (the textarea and the rows own their insets); a
          block-start/-end row (data-align) stacks it into a column. The box wears a chat
          box's look, not a form field's: a 2xl corner, a raised shadow, no hover shift, and a
          focus that only strengthens the border (no ring) - the page's main object, in one
          family with Plane's AI chat input. Plane's focus border there is `accent-subtle`;
          under Prismoid's monochrome accent that token is darker than the resting border, so
          `strong` plays its part. The radius is forced: Propel sets its own from a variable,
          which tailwind-merge does not see as the same property. */}
      <InputGroup
        size="md"
        role="group"
        data-slot="input-group"
        render={(p) => (
          <div
            {...p}
            className={cn(
              p.className,
              "gap-0 px-0 has-[>[data-align=block-end]]:flex-col has-[>[data-align=block-start]]:flex-col",
              "rounded-2xl! shadow-raised-100 hover:border-subtle-1 hover:bg-layer-2",
              // has-[:disabled]: an empty box holds a disabled send button, and Propel reads
              // any disabled descendant as "this field is disabled" and mutes the focus.
              "focus-within:border-strong focus-within:ring-0 focus-within:hover:border-strong has-[:disabled]:focus-within:border-strong has-[:disabled]:focus-within:hover:border-strong",
            )}
          />
        )}
      >
        {children}
      </InputGroup>
    </form>
  );
};

export type PromptInputTextareaProps = ComponentProps<"textarea">;

export const PromptInputTextarea = ({
  onKeyDown,
  className,
  placeholder = "Describe the task…",
  ...props
}: PromptInputTextareaProps) => {
  const handleKeyDown: KeyboardEventHandler<HTMLTextAreaElement> = (e) => {
    onKeyDown?.(e);
    if (e.defaultPrevented) {
      return;
    }
    // Enter submits; Shift+Enter inserts a newline; never submit mid-IME-composition.
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      const { form } = e.currentTarget;
      const submit = form?.querySelector('button[type="submit"]') as HTMLButtonElement | null;
      if (submit?.disabled) {
        return;
      }
      form?.requestSubmit();
    }
  };
  // 14px at every width (the xl size's own body step), so what you type matches the thread's
  // body text; field-sizing grows it in CSS.
  return (
    <TextArea
      size="xl"
      surface="inline"
      render={(p) => (
        <textarea
          {...p}
          data-slot="input-group-control"
          name="message"
          placeholder={placeholder}
          onKeyDown={handleKeyDown}
          {...props}
          className={cn(
            p.className,
            "field-sizing-content max-h-48 min-h-16 w-full px-3 py-2",
            className,
          )}
        />
      )}
    />
  );
};

/** The bottom bar of the composer - tools on the left, submit on the right. */
export type PromptInputToolbarProps = ComponentProps<"div">;

export const PromptInputToolbar = ({ className, ...props }: PromptInputToolbarProps) => (
  // oxlint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-noninteractive-element-interactions -- a pointer shortcut only: a click on the bar's empty space focuses the field, which the keyboard reaches by Tab
  <div
    role="group"
    data-slot="input-group-addon"
    data-align="block-end"
    className={cn(
      "order-last flex h-auto w-full cursor-text items-center justify-between gap-1 px-2 py-2 text-caption-md-medium text-tertiary select-none [&>svg:not([class*='size-'])]:size-3.5",
      className,
    )}
    onClick={(e) => {
      if ((e.target as HTMLElement).closest("button")) {
        return;
      }
      e.currentTarget.parentElement?.querySelector("input")?.focus();
    }}
    {...props}
  />
);

/** A left-aligned cluster of tool buttons within the toolbar. */
export type PromptInputToolsProps = HTMLAttributes<HTMLDivElement>;

export const PromptInputTools = ({ className, ...props }: PromptInputToolsProps) => (
  <div className={cn("flex min-w-0 items-center gap-1", className)} {...props} />
);

// The input group's button sizes: xs/sm label buttons (20/28px), icon-xs/icon-sm squares (24/28px).
type InputGroupButtonSize = "xs" | "sm" | "icon-xs" | "icon-sm";
const GROUP_SIZE = { xs: "xs", sm: "md", "icon-xs": "sm", "icon-sm": "md" } as const;

type InputGroupButtonProps = Omit<KitButtonProps, "variant" | "size" | "square" | "type"> & {
  variant?: KitVariant | null;
  size?: InputGroupButtonSize | null;
  type?: "button" | "submit" | "reset";
};

function InputGroupButton({ type = "button", variant, size, ...props }: InputGroupButtonProps) {
  const s = size ?? "xs";
  return (
    <KitButton
      type={type}
      data-size={s}
      variant={variant ?? "ghost"}
      size={GROUP_SIZE[s]}
      square={s.startsWith("icon")}
      {...props}
    />
  );
}

export type PromptInputButtonTooltip =
  | string
  | {
      content: ReactNode;
      shortcut?: string;
      side?: BaseTooltip.Positioner.Props["side"];
    };

export type PromptInputButtonProps = InputGroupButtonProps & {
  tooltip?: PromptInputButtonTooltip;
};

export const PromptInputButton = ({
  variant = "ghost",
  className,
  size,
  tooltip,
  ...props
}: PromptInputButtonProps) => {
  const resolvedSize = size ?? (Children.count(props.children) > 1 ? "sm" : "icon-sm");
  // An icon button's tooltip is not its name (the popup is not in the tree until hovered), so a
  // plain-text tooltip names an icon-only button unless the caller gave a name.
  const iconOnly = resolvedSize.startsWith("icon");
  const button = (
    <InputGroupButton
      className={className}
      size={resolvedSize}
      type="button"
      variant={variant}
      aria-label={iconOnly && typeof tooltip === "string" ? tooltip : undefined}
      {...props}
    />
  );
  if (!tooltip) {
    return button;
  }
  const content = typeof tooltip === "string" ? tooltip : tooltip.content;
  const shortcut = typeof tooltip === "string" ? undefined : tooltip.shortcut;
  const side = typeof tooltip === "string" ? "top" : (tooltip.side ?? "top");
  // Base UI's tooltip on Propel's popup (Propel's ready-made Tooltip takes only a string label and
  // pins its own 300ms delay; this keeps ReactNode content and the app provider's timing).
  return (
    <BaseTooltip.Root>
      <BaseTooltip.Trigger data-slot="tooltip-trigger" render={button} />
      <BaseTooltip.Portal>
        <BaseTooltip.Positioner
          side={side}
          sideOffset={8}
          className="z-50 outline-none data-anchor-hidden:invisible"
        >
          <BaseTooltip.Popup
            data-slot="tooltip-content"
            render={<TooltipPopup layout="single" />}
          >
            {content}
            {shortcut && <Shortcut keys={shortcut} size="sm" />}
          </BaseTooltip.Popup>
        </BaseTooltip.Positioner>
      </BaseTooltip.Portal>
    </BaseTooltip.Root>
  );
};

export type PromptInputSubmitProps = InputGroupButtonProps & {
  status?: ChatStatus;
  onStop?: () => void;
};

export const PromptInputSubmit = ({
  className,
  variant = "default",
  size = "icon-sm",
  status,
  onStop,
  onClick,
  children,
  ...props
}: PromptInputSubmitProps) => {
  const isGenerating = status === "submitted" || status === "streaming";
  let icon = <CornerDownLeftIcon className="size-4" />;
  if (status === "submitted") {
    icon = <Spinner />;
  } else if (status === "streaming") {
    icon = <SquareIcon className="size-4" />;
  } else if (status === "error") {
    icon = <XIcon className="size-4" />;
  }
  return (
    <InputGroupButton
      aria-label={isGenerating ? "Stop" : "Submit"}
      className={className}
      size={size}
      type={isGenerating && onStop ? "button" : "submit"}
      variant={variant}
      onClick={(e) => {
        if (isGenerating && onStop) {
          e.preventDefault();
          onStop();
          return;
        }
        onClick?.(e);
      }}
      {...props}
    >
      {children ?? icon}
    </InputGroupButton>
  );
};
