/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import type { KeyboardEvent, ReactNode, Ref } from "react";

/** The keyboard half of a clickable surface: Enter or Space on the focused surface itself does
 *  what a click does. A key pressed inside a nested control stays that control's. A sortable
 *  surface passes `["Enter"]`, because Space is the keyboard sensor's lift key there. */
export function pressOnKey(press: () => void, keys: readonly string[] = ["Enter", " "]) {
  return (e: KeyboardEvent<HTMLElement>) => {
    if (e.target !== e.currentTarget || !keys.includes(e.key)) return;
    e.preventDefault();
    press();
  };
}

/** A record's surface in a lens (a gallery tile, a board card, a list row): with `onPress` it
 *  is a focusable button that the pointer and the keyboard both open, and it says which record
 *  the peek shows; without one it is a plain box. The list item around it keeps its list role. */
export function PressSurface({
  onPress,
  selected = false,
  className,
  children,
  ref,
}: {
  onPress?: () => void;
  selected?: boolean;
  className?: string;
  children: ReactNode;
  ref?: Ref<HTMLDivElement>;
}) {
  if (!onPress) {
    return (
      <div ref={ref} className={className}>
        {children}
      </div>
    );
  }
  return (
    <div
      ref={ref}
      role="button"
      tabIndex={0}
      aria-current={selected || undefined}
      onClick={onPress}
      onKeyDown={pressOnKey(onPress)}
      className={`focus-visible:ring-ring outline-none focus-visible:ring-2 ${className ?? ""}`}
    >
      {children}
    </div>
  );
}
