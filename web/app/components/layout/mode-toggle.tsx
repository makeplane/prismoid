/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { Icon } from "@makeplane/propel/components/icon";
import { IconButton } from "@makeplane/propel/components/icon-button";
import { Moon, Sun } from "lucide-react";
import { useTheme } from "next-themes";

/** Cycles light ↔ dark (the icon reflects the resolved theme). System is the default until first toggle. */
export function ModeToggle() {
  const { resolvedTheme, setTheme } = useTheme();
  const isDark = resolvedTheme === "dark";
  return (
    <IconButton
      variant="ghost"
      size="md"
      aria-label="Toggle theme"
      onClick={() => setTheme(isDark ? "light" : "dark")}
      icon={
        // Both glyphs render and CSS picks one, so the server markup never guesses the theme.
        <Icon
          icon={
            <>
              <Sun className="scale-100 rotate-0 transition-all dark:scale-0 dark:-rotate-90" />
              <Moon className="absolute scale-0 rotate-90 transition-all dark:scale-100 dark:rotate-0" />
            </>
          }
        />
      }
    />
  );
}
