/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { Outlet } from "react-router";

/** The standard page chrome: full width, one gutter. No max-width island - the full-bleed
 *  screens (task view, inbox) already reach the window, and a page that floats in the middle
 *  of a wide monitor while its neighbours touch the edges reads as a different app. Pages
 *  that want a reading measure set it on their content (home's composer, the artifact page),
 *  not on the chrome. */
export default function PageContainer() {
  return (
    <div className="w-full px-gutter py-8">
      <Outlet />
    </div>
  );
}
