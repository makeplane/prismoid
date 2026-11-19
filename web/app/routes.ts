/**
 * Copyright (c) 2026-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// TWO applications, one codebase. `APP_TARGET=admin` builds the operator console
// (routes.admin.ts) to its own bundle for its own domain; the default builds the
// product (routes.app.ts). The pick happens here - at build time, in the one file
// react-router reads - so each bundle contains exactly its own app and nothing of
// the other's. This file stays a switch: the maps themselves live beside it.
import adminRoutes from "./routes.admin";
import appRoutes from "./routes.app";

export default process.env.APP_TARGET === "admin" ? adminRoutes : appRoutes;
